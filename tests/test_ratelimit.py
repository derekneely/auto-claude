"""Tests for ratelimit.py — parsing and pause policy for Claude CLI rate limits."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ratelimit import (  # noqa: E402
    MAX_PAUSE_SECONDS,
    RateLimitInfo,
    detect_rate_limit_in_stderr,
    parse_rate_limit_event,
    pause_seconds,
)

# A real event captured from `claude --print --output-format stream-json`
# against subscription auth on CLI 2.1.220 (2026-07-27).
REAL_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1785169800,
        "rateLimitType": "five_hour",
        "overageStatus": "allowed",
        "overageResetsAt": 1785542400,
        "isUsingOverage": False,
    },
    "uuid": "ebfe2cea-71b4-40e7-9e42-214e63da1809",
    "session_id": "ed04ae31-42e2-431e-b2df-25d405873fa0",
}


class TestParseRateLimitEvent:
    def test_parses_the_real_captured_event(self):
        info = parse_rate_limit_event(json.dumps(REAL_EVENT))
        assert info == RateLimitInfo(
            status="allowed",
            limit_type="five_hour",
            resets_at=1785169800,
            is_using_overage=False,
            overage_status="allowed",
        )

    def test_returns_none_for_other_event_types(self):
        assert parse_rate_limit_event('{"type":"assistant","message":{}}') is None
        assert parse_rate_limit_event('{"type":"result","total_cost_usd":0.02}') is None

    def test_returns_none_for_non_json(self):
        assert parse_rate_limit_event("not json at all") is None
        assert parse_rate_limit_event("") is None

    def test_returns_none_when_payload_missing(self):
        assert parse_rate_limit_event('{"type":"rate_limit_event"}') is None

    def test_tolerates_missing_optional_fields(self):
        line = '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}'
        info = parse_rate_limit_event(line)
        assert info is not None
        assert info.status == "rejected"
        assert info.resets_at is None
        assert info.is_using_overage is False

    def test_survives_a_json_array_at_top_level(self):
        # Defensive: json.loads succeeds but the result has no .get
        assert parse_rate_limit_event("[1,2,3]") is None


class TestIsLimited:
    @pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
    def test_permissive_statuses_are_not_limited(self, status):
        assert not RateLimitInfo(status=status).is_limited

    @pytest.mark.parametrize("status", ["rejected", "blocked", "exhausted", "weird"])
    def test_anything_else_is_treated_as_limited(self, status):
        # Fail closed: an unrecognized status pauses rather than hammering the API.
        assert RateLimitInfo(status=status).is_limited


class TestPauseSeconds:
    def test_no_pause_when_not_limited(self):
        info = RateLimitInfo(status="allowed", resets_at=2000)
        assert pause_seconds(info, now=1000) == 0.0

    def test_pauses_until_reset_plus_buffer(self):
        info = RateLimitInfo(status="rejected", resets_at=2000)
        assert pause_seconds(info, now=1000, buffer=60) == 1060.0

    def test_no_negative_pause_when_reset_already_passed(self):
        info = RateLimitInfo(status="rejected", resets_at=500)
        assert pause_seconds(info, now=1000, buffer=60) == 0.0

    def test_falls_back_to_buffer_when_reset_unknown(self):
        # Limited but no resetsAt — still back off rather than spin.
        info = RateLimitInfo(status="rejected", resets_at=None)
        assert pause_seconds(info, now=1000, buffer=60) == 60.0

    def test_caps_absurd_future_timestamps(self):
        # A bogus/far-future resetsAt must not wedge the daemon indefinitely.
        info = RateLimitInfo(status="rejected", resets_at=10**12)
        assert pause_seconds(info, now=1000) == MAX_PAUSE_SECONDS

    def test_handles_milliseconds_timestamps(self):
        # resetsAt is epoch SECONDS; if a future CLI emits millis, don't pause for weeks.
        info = RateLimitInfo(status="rejected", resets_at=1785169800000)
        assert pause_seconds(info, now=1785169700) <= MAX_PAUSE_SECONDS


class TestDetectRateLimitInStderr:
    @pytest.mark.parametrize(
        "text",
        [
            "Error: rate limit exceeded",
            "RATE LIMIT reached, try again later",
            "429 Too Many Requests",
            "Claude usage limit reached",
        ],
    )
    def test_detects_known_phrasings(self, text):
        assert detect_rate_limit_in_stderr(text)

    @pytest.mark.parametrize(
        "text",
        ["", "Exceeded USD budget", "some unrelated failure", "limit of 50 turns"],
    )
    def test_ignores_unrelated_stderr(self, text):
        assert not detect_rate_limit_in_stderr(text)

    def test_does_not_confuse_budget_exhaustion_with_rate_limiting(self):
        # These take different recovery paths; conflating them breaks continuations.
        assert not detect_rate_limit_in_stderr("Error: Exceeded USD budget of 10.0")
