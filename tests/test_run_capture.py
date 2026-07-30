"""Tests for worker.py's Claude-run metrics parsing.

Cost, turns and duration are inputs today (`--max-budget-usd`,
`--max-turns`) but nothing in this codebase has ever parsed the CLI's
`stream-json` `result` event, so `auto_claude.run.cost_usd`/`turns`/
`duration_seconds` would be unfillable without this. Guards: a run with no
result event at all (crash mid-stream) must not raise or fabricate zeros: it
must report all-None so a NULL lands in Postgres, not a misleading 0.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker import (  # noqa: E402
    RunMetrics,
    StateUpdate,
    _accumulate_metrics,
    _parse_run_metrics,
)

# `SimpleNamespace` and `pytest` are unused by the tests below but are
# imported here because this file is built up incrementally across Tasks
# 16-18, and every later task's appended test classes rely on both being
# available at module scope.

RESULT_LINE = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"done","session_id":"s1","uuid":"u1",'
    '"num_turns":12,"duration_ms":45231,"duration_api_ms":40000,'
    '"total_cost_usd":0.8842,"stop_reason":"end_turn",'
    '"terminal_reason":null,"usage":{},"modelUsage":{},'
    '"permission_denials":[],"api_error_status":null,"ttft_ms":900}'
)


class TestParseRunMetrics:
    def test_extracts_cost_turns_and_duration_from_a_normal_result_event(self):
        output = (
            '{"type":"assistant","message":{"content":[]}}\n'
            + RESULT_LINE
        )
        metrics = _parse_run_metrics(output)
        assert metrics.cost_usd == 0.8842
        assert metrics.turns == 12
        assert metrics.duration_seconds == 45  # round(45231 / 1000)

    def test_no_result_event_returns_all_none(self):
        # A crash mid-stream: only assistant/system lines, no terminal result.
        output = '{"type":"system","subtype":"init"}\n{"type":"assistant","message":{}}'
        metrics = _parse_run_metrics(output)
        assert metrics == RunMetrics(cost_usd=None, turns=None, duration_seconds=None)

    def test_empty_output_returns_all_none(self):
        assert _parse_run_metrics("") == RunMetrics()

    def test_malformed_json_lines_are_skipped_not_fatal(self):
        output = "not json at all\n" + RESULT_LINE + "\n{broken\n"
        metrics = _parse_run_metrics(output)
        assert metrics.cost_usd == 0.8842
        assert metrics.turns == 12

    def test_multiple_result_events_the_last_one_wins(self):
        first = RESULT_LINE
        second = RESULT_LINE.replace(
            '"total_cost_usd":0.8842', '"total_cost_usd":1.5'
        ).replace('"num_turns":12', '"num_turns":20')
        output = first + "\n" + second
        metrics = _parse_run_metrics(output)
        assert metrics.cost_usd == 1.5
        assert metrics.turns == 20

    def test_missing_keys_on_the_result_event_are_none_not_fatal(self):
        output = '{"type":"result","subtype":"success"}'
        metrics = _parse_run_metrics(output)
        assert metrics == RunMetrics()


class TestAccumulateMetrics:
    def test_sums_two_complete_readings(self):
        base = RunMetrics(cost_usd=1.0, turns=5, duration_seconds=10)
        extra = RunMetrics(cost_usd=0.25, turns=2, duration_seconds=3)
        total = _accumulate_metrics(base, extra)
        assert total == RunMetrics(cost_usd=1.25, turns=7, duration_seconds=13)

    def test_a_none_reading_does_not_poison_the_other(self):
        base = RunMetrics(cost_usd=1.0, turns=5, duration_seconds=10)
        extra = RunMetrics()  # repair round crashed before its own result event
        total = _accumulate_metrics(base, extra)
        assert total == RunMetrics(cost_usd=1.0, turns=5, duration_seconds=10)

    def test_both_none_stays_none(self):
        assert _accumulate_metrics(RunMetrics(), RunMetrics()) == RunMetrics()
