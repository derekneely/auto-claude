"""db/journal.py — the append-only fallback for writes made while Postgres
was unreachable.

One JSON object per line: {"op": <str>, "payload": <dict>}. Every op maps to
an idempotent call from db/history.py, db/issue_state.py or db/harness.py
(ON CONFLICT DO NOTHING inserts, last-writer-wins updates), because replay
may legitimately run the same entry twice — see `replay()`.

Lease ops (db/lease.py's acquire/heartbeat/release/check/release_expired)
are deliberately NOT journalable and have no entry in `OPS` or
`_OP_HANDLERS`. A lease claim or fence check is a compare-and-swap against
"right now" — replaying a stale claim later would hand an issue to a
harness that no longer owns it, so `DbSync`'s lease methods bypass
`_durable`/this journal entirely and always talk straight to Postgres.

`replay()` draws a hard line between two kinds of failure:

- Transient (`DbUnavailable`): Postgres, not the entry, is at fault. This
  must stop the loop and leave the journal file completely untouched —
  that is the confirm-then-truncate guarantee (see `replay()`) and it must
  never regress.
- Structural (unparseable JSON, a missing `op`/`payload` key, an
  unrecognised op string, or a payload whose keys don't match its
  handler's signature): no future replay of *this* entry could ever
  succeed. Raising uncaught here would wedge the journal on that one line
  forever — every retry re-applies the entries ahead of it, hits the same
  fault, and never drains the entries behind it, silently losing every
  write queued behind a single torn line. Structurally bad lines are
  instead quarantined verbatim to a sibling `.corrupt` file and logged
  once at error level, so an operator can see and recover them, and
  replay proceeds past them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from db import harness as db_harness
from db import history as db_history
from db import issue_state as db_issue_state
from db.pool import Database, DbUnavailable

_OP_HANDLERS: dict[str, Callable[[Database, dict], None]] = {
    "issue_state.upsert": lambda db, payload: db_issue_state.upsert(db, **payload),
    "history.start_run": lambda db, payload: db_history.start_run(db, **payload),
    "history.finish_run": lambda db, payload: db_history.finish_run(db, **payload),
    "history.add_summary": lambda db, payload: db_history.add_summary(db, **payload),
    "harness.register": lambda db, payload: db_harness.register(
        db, db_harness.Harness(**payload)
    ),
}

_logger = logging.getLogger(__name__)


class Journal:
    """Append-only JSONL of writes made while Postgres was unreachable.

    One JSON object per line: {"op": <str>, "payload": <dict>}.
    Every op must be idempotent, because replay may run twice.
    """

    # Generated from _OP_HANDLERS rather than hand-copied: two independently
    # maintained lists of the same op names will eventually drift, and an op
    # added to one but not the other silently wedges append() or replay().
    OPS = tuple(_OP_HANDLERS)

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, op: str, payload: dict) -> None:
        if op not in self.OPS:
            raise ValueError(f"Unknown journal op: {op!r}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"op": op, "payload": payload})
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def pending(self) -> int:
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8-sig") as f:
            return sum(1 for line in f if line.strip())

    def replay(self, db: Database) -> int:
        """Apply every entry in order, then truncate. Returns entries applied.

        Raises DbUnavailable without touching the file if the DB drops
        mid-replay — every entry, including the ones already applied before
        the drop, stays in the journal so the next successful replay
        re-runs them too. That is safe only because every handler above is
        idempotent; this is the one place in the codebase allowed to rely
        on that.

        Confirm-then-truncate, never the reverse: truncating before every
        entry is known to be resolved would mean a connection drop on entry
        3 of 5 discards entries 4 and 5 along with the ones that already
        succeeded, with no other copy to recover them from — Postgres itself
        is exactly what was unreachable. This now covers quarantine
        decisions too: a structurally bad line found earlier in this pass
        is only moved to `.corrupt` once the *entire* pass finishes without
        a transient failure — a `DbUnavailable` on a later entry leaves that
        bad line exactly where it was, so the next attempt re-evaluates it
        from scratch instead of finding it half-migrated.
        """
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8-sig") as f:
            lines = [line.strip() for line in f if line.strip()]
        if not lines:
            return 0

        applied = 0
        quarantined: list[tuple[str, str]] = []  # (raw line, why it was rejected)
        for line in lines:
            try:
                entry = json.loads(line)
                op = entry["op"]
                payload = entry["payload"]
            except (json.JSONDecodeError, KeyError) as exc:
                # Torn write, or a hand-edited/foreign line missing a
                # required key — no retry will ever parse this differently.
                quarantined.append((line, f"{type(exc).__name__}: {exc}"))
                continue

            handler = _OP_HANDLERS.get(op)
            if handler is None:
                # An op this version of the daemon doesn't recognise —
                # structural, not transient.
                quarantined.append((line, f"unknown op {op!r}"))
                continue

            try:
                handler(db, payload)
            except DbUnavailable:
                # Transient: Postgres, not the entry, is at fault. Propagate
                # uncaught so nothing below runs — the journal, and every
                # quarantine decision tentatively made above this line in
                # this same pass, is left exactly as it was. See docstring.
                raise
            except TypeError as exc:
                # The payload's keys don't match this handler's real
                # signature (e.g. a stale field from an older version of
                # this daemon) — raised by the kwarg splat before db.execute
                # is ever reached. Structural, not a database problem.
                quarantined.append((line, f"TypeError: {exc}"))
                continue
            applied += 1

        # The whole pass resolved every line — applied or quarantined — with
        # nothing transient interrupting us. Safe to commit both at once.
        if quarantined:
            corrupt_path = self._path.with_suffix(".corrupt")
            with corrupt_path.open("a", encoding="utf-8") as f:
                for line, _reason in quarantined:
                    f.write(line + "\n")  # verbatim — an operator may need it
            _logger.error(
                "journal.replay: quarantined %d unreplayable entr%s to %s: %s",
                len(quarantined),
                "y" if len(quarantined) == 1 else "ies",
                corrupt_path,
                "; ".join(reason for _line, reason in quarantined),
            )

        # Written explicitly as UTF-8, no BOM.
        self._path.write_text("", encoding="utf-8")
        return applied
