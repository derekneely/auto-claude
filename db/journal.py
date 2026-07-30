"""db/journal.py — the append-only fallback for writes made while Postgres
was unreachable.

One JSON object per line: {"op": <str>, "payload": <dict>}. Every op maps to
an idempotent call from db/history.py, db/issue_state.py or db/harness.py
(ON CONFLICT DO NOTHING inserts, last-writer-wins updates), because replay
may legitimately run the same entry twice — see `replay()`.

Lease ops (db/lease.py's acquire/heartbeat/release/check/release_expired)
are deliberately NOT journalable and have no entry in `OPS` or
`_OP_SPECS`. A lease claim or fence check is a compare-and-swap against
"right now" — replaying a stale claim later would hand an issue to a
harness that no longer owns it, so `DbSync`'s lease methods bypass
`_durable`/this journal entirely and always talk straight to Postgres.

`replay()` draws a hard line between two kinds of failure:

- Transient (`DbUnavailable`, or any other exception genuinely raised from
  *inside* a handler's real work — e.g. a future bug in a handler, or a
  parameter-adaptation error deep inside `db.execute`): the entry itself
  was fine; something else went wrong applying it. This must stop the
  loop and leave the journal file completely untouched — that is the
  confirm-then-truncate guarantee (see `replay()`) and it must never
  regress.
- Structural (unparseable JSON, an entry that isn't a JSON object, a
  missing/non-string `op`, a missing `payload`, an unrecognised op
  string, or a payload whose keys don't match its op's real signature):
  no future replay of *this* entry could ever succeed no matter how many
  times it's retried. Raising uncaught here would wedge the journal on
  that one line forever — every retry re-applies the entries ahead of
  it, hits the same fault, and never drains the entries behind it,
  silently losing every write queued behind a single bad line.
  Structurally bad lines are instead quarantined verbatim to a sibling
  `.corrupt` file and logged once at error level, so an operator can see
  and recover them, and replay proceeds past them.

A payload's structural fitness is checked with `inspect.Signature.bind`
against the *real* target (the plain function for issue_state/history ops,
the `Harness` dataclass constructor for `harness.register`) as a step
separate from actually invoking the handler. This matters: it lets a
kwarg mismatch be recognised and quarantined as data, while a `TypeError`
raised from inside the handler's real body — a genuine coding defect, not
a bad payload — still propagates uncaught like any other unexpected
failure, instead of being silently and incorrectly filed away as "bad
data".
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from db import harness as db_harness
from db import history as db_history
from db import issue_state as db_issue_state
from db.pool import Database


@dataclass(frozen=True)
class _OpSpec:
    # Never invoked — used only to validate a payload's shape against the
    # real target's signature before `invoke` is ever called.
    signature_target: Callable
    invoke: Callable[[Database, dict], None]


_OP_SPECS: dict[str, _OpSpec] = {
    "issue_state.upsert": _OpSpec(
        signature_target=db_issue_state.upsert,
        invoke=lambda db, payload: db_issue_state.upsert(db, **payload),
    ),
    "history.start_run": _OpSpec(
        signature_target=db_history.start_run,
        invoke=lambda db, payload: db_history.start_run(db, **payload),
    ),
    "history.finish_run": _OpSpec(
        signature_target=db_history.finish_run,
        invoke=lambda db, payload: db_history.finish_run(db, **payload),
    ),
    "history.add_summary": _OpSpec(
        signature_target=db_history.add_summary,
        invoke=lambda db, payload: db_history.add_summary(db, **payload),
    ),
    "harness.register": _OpSpec(
        # The payload's shape must match Harness's fields, not register()'s
        # own (db, harness) signature — register() itself never rejects a
        # payload, only Harness(**payload) can.
        signature_target=db_harness.Harness,
        invoke=lambda db, payload: db_harness.register(
            db, db_harness.Harness(**payload)
        ),
    ),
}

_logger = logging.getLogger(__name__)


def _payload_matches_signature(op: str, payload: object) -> str | None:
    """None if `payload` structurally fits `op`'s real target; otherwise a
    human-readable reason. Never calls the target — `Signature.bind` only
    checks arity/keyword-name/mapping-ness, so this cannot have side
    effects even for a payload that would otherwise be dangerous."""
    target = _OP_SPECS[op].signature_target
    try:
        if target is db_harness.Harness:
            inspect.signature(target).bind(**payload)  # type: ignore[arg-type]
        else:
            # Every other target's first parameter is `db` — a placeholder
            # stands in for it since binding never calls the target.
            inspect.signature(target).bind(object(), **payload)  # type: ignore[arg-type]
    except TypeError as exc:
        return str(exc)
    return None


class Journal:
    """Append-only JSONL of writes made while Postgres was unreachable.

    One JSON object per line: {"op": <str>, "payload": <dict>}.
    Every op must be idempotent, because replay may run twice.
    """

    # Generated from _OP_SPECS rather than hand-copied: a second,
    # independently maintained list of the same op names would eventually
    # drift, and an op added to one but not the other silently wedges
    # append() or replay().
    OPS = tuple(_OP_SPECS)

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

        Raises uncaught, without touching the file, on anything transient —
        `DbUnavailable`, or any other exception a handler's real work
        genuinely raises: every entry, including the ones already applied
        before the failure, stays in the journal so the next successful
        replay re-runs them too. That is safe only because every handler is
        idempotent; this is the one place in the codebase allowed to rely
        on that.

        Confirm-then-truncate, never the reverse: truncating before every
        entry is known to be resolved would mean a failure on entry 3 of 5
        discards entries 4 and 5 along with the ones that already
        succeeded, with no other copy to recover them from — Postgres
        itself is exactly what was unreachable. This covers quarantine
        decisions too: a structurally bad line found earlier in this pass
        is only moved to `.corrupt` once the *entire* pass finishes without
        a transient failure, and only once that move is itself durably
        written — see below.
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
            except json.JSONDecodeError as exc:
                # A torn write (crash/power-loss mid-append) or otherwise
                # invalid JSON — no retry will ever parse this differently.
                quarantined.append((line, f"JSONDecodeError: {exc}"))
                continue

            # A bare JSON scalar/array (`42`, `null`, `[1,2,3]`), or an
            # object missing a string `op` or a `payload` key, is checked
            # explicitly here — not via a broad except — so that indexing
            # or hashing it can never itself raise an uncaught TypeError
            # further down (e.g. `entry["op"]` on a list, or a non-string,
            # unhashable `op` value reaching a dict lookup).
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("op"), str)
                or "payload" not in entry
            ):
                quarantined.append((line, f"malformed journal entry: {entry!r}"))
                continue

            op = entry["op"]
            payload = entry["payload"]

            if op not in _OP_SPECS:
                # An op this version of the daemon doesn't recognise —
                # structural, not transient.
                quarantined.append((line, f"unknown op {op!r}"))
                continue

            mismatch = _payload_matches_signature(op, payload)
            if mismatch is not None:
                # The payload's keys don't match this op's real signature
                # (e.g. a stale field from an older version of this
                # daemon) — caught by binding, before the real handler (and
                # therefore db.execute) is ever touched. Structural, not a
                # database problem, and not the same thing as a TypeError
                # raised from *inside* the handler below, which is a real
                # bug and must propagate rather than be quarantined here.
                quarantined.append((line, f"payload mismatch for {op}: {mismatch}"))
                continue

            # Deliberately no try/except here: DbUnavailable, or any other
            # exception genuinely raised while doing the real work (a
            # future bug in a handler, a parameter-adaptation failure deep
            # inside db.execute), propagates uncaught. The payload already
            # passed a structural check above, so a failure here is about
            # the database or the code, not the data — exactly the case
            # that must stop the loop and leave the journal untouched.
            _OP_SPECS[op].invoke(db, payload)
            applied += 1

        # The whole pass resolved every line — applied or quarantined — with
        # nothing transient interrupting us. Safe to commit both at once.
        if quarantined:
            corrupt_path = self._path.with_suffix(".corrupt")
            try:
                with corrupt_path.open("a", encoding="utf-8") as f:
                    for line, _reason in quarantined:
                        f.write(line + "\n")  # verbatim — an operator may need it
            except OSError as exc:
                # We could not durably record the quarantine decision, so
                # we must not truncate either: a quarantine we could not
                # persist must never license discarding the source line.
                # Everything already applied above is idempotent (ON
                # CONFLICT DO NOTHING / last-writer-wins), so leaving the
                # journal untouched and retrying this whole pass again
                # next time is harmless — the alternative (truncating
                # anyway) would permanently lose the quarantined lines
                # with no copy anywhere, which is the exact failure this
                # module exists to prevent.
                _logger.error(
                    "journal.replay: could not write %d quarantined entr%s to "
                    "%s (%s) — leaving the journal untouched so the next "
                    "replay retries everything",
                    len(quarantined),
                    "y" if len(quarantined) == 1 else "ies",
                    corrupt_path,
                    exc,
                )
                return applied
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


class NullJournal:
    """A `Journal` stand-in whose `append` always raises.

    For a caller that must satisfy `DbSync`'s required `journal` constructor
    argument but must never actually be able to queue a durable write —
    `worker._assert_lease_held` builds its own throwaway `DbSync` purely to
    call `check_lease`, which per the spec ("Claims and fence checks never
    queue") never journals in the first place, and a handful of tests
    exercise a `DbSync` on a path that always succeeds before ever reaching
    `_durable`'s failure branches.

    A real `Journal` on some placeholder path would happen to work for both
    of those today, because nothing on either path currently calls
    `.append()` - but that makes "this DbSync must never journal" a usage
    convention rather than something the type system enforces. The day a
    durable write is added to a worker by mistake, a real `Journal` would
    silently append cross-process into `main`'s actual `journal.jsonl`
    (racing `Journal.replay`'s read-then-truncate — see its own docstring),
    while `NullJournal` fails loudly and immediately instead.
    """

    def append(self, op: str, payload: dict) -> None:
        raise RuntimeError(
            f"NullJournal.append({op!r}) called — this DbSync must never "
            f"journal a durable write; see NullJournal's docstring."
        )
