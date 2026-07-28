"""Rate-limit detection and backoff policy for the Claude CLI.

Under subscription auth the binding constraint is not `--max-budget-usd` but the
rolling usage window. The CLI reports it inline on the stream-json channel:

    {"type":"rate_limit_event","rate_limit_info":{
       "status":"allowed","resetsAt":1785169800,"rateLimitType":"five_hour",
       "overageStatus":"allowed","overageResetsAt":1785542400,
       "isUsingOverage":false}}

These events are emitted on every run, almost always with status "allowed" — they
are informational until the status changes. This module keeps the parsing pure so
it can be tested without spawning a subprocess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Statuses that mean "keep working". Anything else is treated as limited, so an
# unrecognized value fails closed (pause) rather than open (hammer the API).
OK_STATUSES = frozenset({"allowed", "allowed_warning"})

# Never pause longer than this, whatever the payload claims. The window is five
# hours; a larger value means a bogus timestamp, and an unbounded pause would
# silently wedge the daemon.
MAX_PAUSE_SECONDS = 6 * 60 * 60

# Default grace added after resetsAt before resuming.
DEFAULT_BUFFER_SECONDS = 60

# resetsAt is epoch SECONDS. Anything above this is implausible as seconds
# (year ~5138) and is assumed to be milliseconds.
_MILLIS_THRESHOLD = 10**11

# Substrings that indicate rate limiting when the run fails without a usable
# event on stdout. Deliberately excludes budget exhaustion, which has its own
# recovery path (continuation runs) and must not be conflated.
_STDERR_MARKERS = (
    "rate limit",
    "rate_limit",
    "429",
    "usage limit",
    "too many requests",
)


@dataclass(frozen=True)
class RateLimitInfo:
    """A parsed `rate_limit_event` payload."""

    status: str
    limit_type: str | None = None
    resets_at: int | None = None
    is_using_overage: bool = False
    overage_status: str | None = None

    @property
    def is_limited(self) -> bool:
        """True when this event means work should stop."""
        return self.status not in OK_STATUSES


def parse_rate_limit_event(line: str) -> RateLimitInfo | None:
    """Parse one stream-json line into a RateLimitInfo, or None.

    Returns None for any line that is not a well-formed rate_limit_event —
    including malformed JSON, since this runs over untrusted subprocess output.
    """
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict) or data.get("type") != "rate_limit_event":
        return None

    payload = data.get("rate_limit_info")
    if not isinstance(payload, dict):
        return None

    resets_at = payload.get("resetsAt")
    if not isinstance(resets_at, (int, float)):
        resets_at = None
    else:
        resets_at = int(resets_at)
        if resets_at > _MILLIS_THRESHOLD:
            resets_at //= 1000

    return RateLimitInfo(
        status=str(payload.get("status", "")),
        limit_type=payload.get("rateLimitType"),
        resets_at=resets_at,
        is_using_overage=bool(payload.get("isUsingOverage", False)),
        overage_status=payload.get("overageStatus"),
    )


def detect_rate_limit_in_stderr(stderr: str) -> bool:
    """Fallback detection for runs that fail without emitting a usable event."""
    if not stderr:
        return False
    lowered = stderr.lower()
    return any(marker in lowered for marker in _STDERR_MARKERS)


def pause_seconds(
    info: RateLimitInfo,
    now: float,
    buffer: int = DEFAULT_BUFFER_SECONDS,
) -> float:
    """How long to stop spawning workers, given a rate-limit event.

    Returns 0.0 when the event does not call for a pause.
    """
    if not info.is_limited:
        return 0.0

    if info.resets_at is None:
        # Limited but no reset time — back off by the buffer rather than spin.
        return float(buffer)

    wait = (info.resets_at + buffer) - now
    if wait <= 0:
        return 0.0
    return float(min(wait, MAX_PAUSE_SECONDS))
