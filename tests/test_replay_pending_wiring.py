# tests/test_replay_pending_wiring.py
"""Fix round 1, Finding 4: `dbsync.replay_pending()` must be called exactly
once per poll-loop pass, alongside the top-of-loop `_maybe_heartbeat` call —
never from the per-second sleep tick main.py's step 7 uses to keep shutdown
responsive.

`_maybe_heartbeat` is interval-gated (see tests/test_heartbeat.py) precisely
so that tick never blocks on a dead database. `replay_pending()` has no such
gate: `Database(retries=2, connect_timeout=10)` (db/pool.py) means one
failing `execute()` during replay burns roughly 33 seconds of backoff, and
that cost only materialises when the journal is non-empty — exactly when
Postgres is down, since `Journal.replay` short-circuits on an empty journal.
Calling it from the sleep tick would defeat "sleep in small increments so
shutdown is responsive" on every one of those ticks.

This is a source-text meta-test, mirroring test_push_guard.py's style,
because main()'s poll loop is a large integrated function that is not
decomposed into an independently callable unit — asserting "only one call
site, and it's the one attached to the interval-gated heartbeat" is what is
actually checkable without running main() itself (which would need a real
Postgres, a real GitHub client, and multiprocessing — all off-limits here).
"""

from __future__ import annotations

from pathlib import Path

_MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


def test_replay_pending_is_called_exactly_once():
    source = _MAIN_PY.read_text(encoding="utf-8")
    call_sites = source.count("replay_pending()")
    assert call_sites == 1, (
        f"found {call_sites} call site(s) for replay_pending() in main.py — "
        "it must be called exactly once, immediately after the top-of-loop "
        "_maybe_heartbeat call, and never from the per-second sleep tick "
        "(fix round, Finding 4)"
    )


def test_replay_pending_call_sits_next_to_the_top_of_loop_heartbeat():
    source = _MAIN_PY.read_text(encoding="utf-8")
    # The sleep-tick call site used to sit directly after a `time.sleep(1)`-
    # adjacent _maybe_heartbeat call inside the "Sleep in small increments"
    # block; guard against it quietly coming back by requiring the one
    # remaining call to immediately follow a _maybe_heartbeat(...) call that
    # is NOT the one inside that block. Simplest reliable signal: the sole
    # call site must appear before the "Sleep in small increments" comment,
    # i.e. in the top-of-loop section, not after it.
    sleep_marker = source.index("Sleep in small increments")
    replay_index = source.index("replay_pending()")
    assert replay_index < sleep_marker, (
        "replay_pending() must be called at the top of the poll loop, "
        "before the per-second sleep tick — not inside it"
    )
