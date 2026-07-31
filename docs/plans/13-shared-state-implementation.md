# Shared State in Postgres — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move auto-claude's portable state into Postgres so a dead box is recoverable by another harness, and so a review interface becomes possible later.

**Architecture:** A new `auto_claude` schema on the existing `PIPELINE_METRICS_DATABASE_URL` holds the lease, portable counters, run history and summaries. `state/issues.json` is demoted from source-of-truth to a disposable local cache, rebuilt at every startup from GitHub labels plus Postgres. Issues are claimed with a single atomic `UPDATE ... RETURNING` lease; a running agent is never aborted for a lost lease, but the three irreversible acts (push, PR, label writes) are fenced behind a lease re-check. `main` remains the only Postgres writer, reusing the existing `StateUpdate` queue.

**Tech Stack:** Python 3.14, psycopg 3 (raw SQL at runtime), Alembic + SQLAlchemy (migrations only), pytest. Postgres 17.6 on Supabase.

**Spec:** `docs/plans/12-shared-state-in-postgres.md`. That document is the design and this plan does not repeat it. Where this plan makes a decision the spec left open, it is called out under Design Decisions below.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **Python 3.11+** floor; the dev box runs **3.14.3**. All wheels verified available for cp314/win_amd64.
- **PowerShell is primary.** Every command in this plan uses `.venv\Scripts\python`. The Bash tool is Git Bash and its cwd drifts — prefer absolute paths.
- **Dependencies live in a local `.venv`** created in Task 1, pinned in `requirements.txt`. The daemon is launched as `.venv\Scripts\python main.py` from Task 1 onward. This retires the README's "zero dependencies" claim.
- **`python -m pytest tests/ -q` must pass after every single task.** The baseline before Task 1 is **493 passed**. Each task states its own expected running total.
- **No test touches a real database, network or subprocess**, with exactly one exception: `tests/test_lease_concurrency.py`, which is marked `@pytest.mark.postgres` and skips unless `AUTO_CLAUDE_TEST_DATABASE_URL` is set. Task 4's Alembic verification is a manual command, not a test.
- **Every `subprocess.run`/`Popen` with `text=True` MUST also pass `encoding="utf-8", errors="replace"` and `env=build_env(current_token())`.** `text=True` alone decodes as cp1252 and has crashed this daemon on smart quotes. Enforced by `tests/test_subprocess_encoding.py`, which scans source text — do not regress it.
- **`tests/test_push_guard.py:200-210` counts `'"git", "push"'` against `'assert_pushable('` in `worker.py` source text.** Adding a fence call is invisible to it; adding a new push site is not.
- **Read `.env` and token files as `utf-8-sig`.** PowerShell writes BOMs and `str.strip()` does not remove `﻿`. When appending to `.env*`, use `-Encoding utf8NoBOM`.
- **`GH_TOKEN` governs `gh` but not `git`.** Network git commands must go through `ghauth.apply_git_credentials()`. The token env var is `AUTO_CLAUDE_GH_TOKEN`, deliberately not `GH_TOKEN`.
- **`multiprocessing` uses the `spawn` start method.** Anything crossing the process boundary — `IssueContext`, `StateUpdate` — must be picklable. No connections, file handles or lambdas.
- **Never abort a running Claude agent** for a lost lease or a database outage. This is a hard rule from the spec.
- **The daemon never runs migrations.** It checks the schema version at startup and refuses to start if it is behind, printing the exact command to run.
- **Commits:** conventional commit with an `(ai-cc)` suffix in the subject, and **no `Co-Authored-By` trailer**. One commit per task.
- **Do not start the daemon to check something.** `field_admin#215` is parked deliberately and is the end-to-end test after this plan is built, not before.

---

## Verified environment facts

Established by probing before this plan was written; do not re-derive.

| Fact | Value |
|---|---|
| Database | Supabase **PostgreSQL 17.6** |
| Host / port | `aws-1-us-east-1.pooler.supabase.com` : **5432** |
| Pooler mode | **Session** mode (5432). *Not* the 6543 transaction pooler — so prepared statements and session state are safe. If this URL is ever moved to 6543, psycopg's prepared statements break. |
| User | `postgres`, with `CREATE` privilege on the database |
| Must remain untouched | `public.pipeline_events`, `public.pipeline_schema_migrations` (owned by the Node toolchain) |
| Wheels for cp314/win_amd64 | psycopg 3.3.4, psycopg_binary 3.3.4, alembic 1.18.5, SQLAlchemy 2.0.51 |
| Claude CLI | 2.1.220 |

**Claude CLI `stream-json` result event** — probed from the real CLI, because nothing in the codebase parses it today. A line with `"type":"result"` carries:

```
type, subtype, is_error, result, session_id, uuid,
num_turns, duration_ms, duration_api_ms, total_cost_usd,
stop_reason, terminal_reason, usage{...}, modelUsage{...},
permission_denials, api_error_status, ttft_ms
```

So `cost_usd = total_cost_usd`, `turns = num_turns`, `duration_seconds = round(duration_ms / 1000)`.

---

## Design decisions layered on the spec

The spec left these open. They are fixed for this plan; a task must not re-litigate them.

1. **Runtime SQL is raw psycopg 3.** SQLAlchemy is present only because Alembic needs it. No ORM, no models — the lease claim is the spec's own hand-written statement.
2. **`issue_state.stage` holds the GitHub `ac-*` stage label, not the local `IssueStatus`.** `issue_state.kind` holds `fix|implement|test|rework` (i.e. `IssueRecord.action`). The local `IssueStatus` is **never persisted to Postgres**; reconciliation derives it from stage plus lease. This is what makes the stranded-`in_progress` failure of 2026-07-29 impossible by construction rather than by a fix.
3. **All `id` primary keys are `text` holding `uuid.uuid4().hex`,** generated by the harness. Journal payloads round-trip through JSON, so a native `uuid` column would force a conversion at every call site for no benefit at this scale.
4. **All timestamps are server-side** (`now()`, `DEFAULT now()`, `timestamptz`). Lease comparisons must never mix clocks across machines.
5. **The fence fails closed, with a bounded retry.** `lease.check` retries 3× over ~5s; if the database is still unreachable, the lease is treated as **lost** and the irreversible act is refused. A failed fence check cannot distinguish "we are partitioned" from "someone else took over", and the work is not destroyed — the branch stays local, a crash log and a summary explain it, and the issue is retried. **Claims and fence checks never journal**, per the spec.
6. **Writes reach Postgres through `StateStore.on_change`,** installed once in `main`. Making it automatic is the point: roughly twenty existing `state.save()` sites would otherwise each need a database call remembered. The 22 existing test files use `FakeState` and are unaffected.
7. **`db/` is a package** — the one deviation from this repo's flat module layout — because it is eight cohesive new modules. `import db` works with the existing `sys.path.insert` test bootstrap.

### Two open items this plan deliberately does not solve

- **`config.database.lease_ttl_seconds` is honoured, but `pipeline.json`'s per-repo `staleLockHours` is not.** The lease TTL is global to the harness; a per-repo TTL would mean a per-repo lease policy, which nothing has asked for. Task 14 turns `staleLockHours` into a startup diagnostic that warns when it disagrees with the configured TTL, rather than leaving it silently parsed and ignored.
- **Cost and turns have never been captured.** The spec's `run` table assumes they exist; they do not. Task 16 adds the parsing. Historical runs are not backfilled — inventing that data would put fiction in the interface, which the spec forbids.

---

## Sequencing and the `dbsync` dependency

The spec sequences the local journal at step 5, last before the interface. Taken literally that is impossible: `DbSync` is the seam `main`, `process_manager` and `worker` all code against from Phase B onward, and `DbSync`'s constructor takes a `Journal`.

`DbSync` also cannot simply be moved earlier — it is a facade over *every* `db/` module, so it cannot precede them.

**Resolution: `dbsync.py` is introduced when its first consumer needs it (Task 8) and grows as each module lands.** It is created with only the methods whose backing module exists — `enabled`, `upsert_issue` — and a log-and-drop failure sink. Each later task adds its own methods alongside the module it introduces:

| Task | Adds to `DbSync` |
|---|---|
| 8 | `enabled`, `upsert_issue`, log-and-drop sink |
| 13 | `acquire_lease`, `heartbeat`, `release_lease`, `check_lease`, `release_expired` |
| 18 | `start_run`, `finish_run` |
| 19 | `add_summary` |
| 21 | a real `Journal` replaces log-and-drop; `replay_pending` |

Every signature is frozen from the start, so no task ever changes one another task already calls. Until Task 21, a write that cannot reach Postgres is logged and discarded — which is safe, because GitHub labels remain truth and startup reconciliation rebuilds the cache from scratch. The spec's *intent* — that durable offline queuing is a later increment than the lease — is preserved exactly.

```
Phase A  Tasks 1-5    foundation: venv, config, pool, Alembic, schema gate
Phase B  Tasks 6-11   harness, issue_state, DbSync, reconciliation; issues.json demoted to cache
Phase C  Tasks 12-15  lease + fencing, as one change; replaces _release_stale_locks
Phase D  Tasks 16-19  run + summary capture
Phase E  Tasks 20-21  journal + replay
Step 6   the interface — a separate project, out of scope here
```

Tasks 1-15 deliver resilience and are independently testable end to end on `field_admin#215`. Tasks 16-21 deliver the history the interface will read.

**Degraded operation is a first-class path — but only when Postgres is deliberately absent, not when it is broken.** Startup and runtime are governed by different rules, and conflating them is a defect:

| Condition | Startup | Runtime (daemon already up) |
|---|---|---|
| `[database].enabled = false`, or no URL set | **Start.** Single-harness, `issues.json`-backed, no leases — exactly as today. | Unchanged; nothing ever calls Postgres. |
| Postgres **unreachable** | **Refuse to start.** Log the reason and exit non-zero. | **Never abort a running agent.** Durable writes log-and-drop (later, journal); the daemon keeps working. |
| Postgres reachable, schema **stale or missing** | **Refuse to start**, printing the exact upgrade command. The daemon never migrates. | n/a |

The startup refusal is a deliberate ruling (2026-07-29): an unreachable database at boot means this harness cannot take a lease, and a harness that cannot take a lease could double-claim an issue another box is already working. A blip that stops autonomous work until a human looks is preferred over two harnesses on one issue. Runtime is the opposite trade — work already in flight is never thrown away for a database problem, which the spec states outright.

Every task that touches startup tests both halves of this explicitly.

---

## File structure

| File | Action | Responsibility |
|---|---|---|
| `requirements.txt` | create | runtime + test dependencies |
| `.gitignore` | modify | add `.venv/`, `state/journal.jsonl` |
| `README.md` | modify | install section; retires the zero-dependency claim |
| `version.py` | create | `__version__`, recorded on the harness row |
| `alembic.ini` | create | Alembic config |
| `migrations/env.py` | create | creates the schema, sets `version_table_schema` |
| `migrations/versions/0001_initial.py` | create | all four tables |
| `db/pool.py` | create | `Database`, `DbUnavailable` — connection, retry, reconnect |
| `db/schema.py` | create | expected revision, startup currency gate |
| `db/harness.py` | create | harness identity row + liveness |
| `db/issue_state.py` | create | portable per-issue columns; never touches lease columns |
| `db/lease.py` | create | acquire / heartbeat / release / check / expire |
| `db/history.py` | create | `run` and `summary` inserts, all replay-safe |
| `db/journal.py` | create | append-only local queue and idempotent replay |
| `dbsync.py` | create | the single seam `main`, `process_manager` and `worker` use |
| `reconcile.py` | create | rebuild `issues.json` from GitHub + Postgres |
| `config.py` | modify | `DatabaseConfig` + `[database]` section |
| `state.py` | modify | `StateStore(state_file, on_change=None)` |
| `main.py` | modify | startup wiring, heartbeat, expiry-driven stale locks |
| `process_manager.py` | modify | lease before spawn, run rows, release on reap |
| `worker.py` | modify | fencing at the irreversible acts, result-event parsing |

---
## Phase A — Foundation

### Task 1: venv + requirements.txt + .gitignore + README install section + version.py

**Files:**
- Create: `version.py`
- Create: `requirements.txt`
- Modify: `.gitignore`
- Modify: `README.md`
- Test: `tests/test_version.py`

**Interfaces:**
- Consumes: nothing
- Produces: `version.__version__: str` — consumed by Task 6 (`db/harness.new_harness(version)`) and wired in by Task 11 (`main.py` passes `version.__version__` to `new_harness`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_version.py
"""Tests for version.py — the harness identity string recorded in
auto_claude.harness.version by db/harness.py (Task 6).

A harness with no discoverable version would make `run.model` / `harness.version`
useless for telling "which build produced this row" apart during an incident,
so the format is locked down here rather than left to drift.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import version  # noqa: E402


class TestVersion:
    def test_dunder_version_is_defined(self):
        assert hasattr(version, "__version__")

    def test_dunder_version_matches_semver(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__)
```

- [ ] **Step 2: Run the test to verify it fails**

Run (no venv exists yet, so use the system interpreter for this one check only): `python -m pytest tests/test_version.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'version'`

- [ ] **Step 3: Write the implementation**

```python
# version.py
"""Single source of truth for auto-claude's version.

Recorded verbatim in `auto_claude.harness.version` (db/harness.py, Task 6) so
an incident review can tell which build produced a given row. Bump this by
hand on every release that changes on-disk or on-wire behavior worth
distinguishing.
"""

__version__ = "0.2.0"
```

```text
# requirements.txt — runtime + test deps. Wheels confirmed available for
# cp314/win_amd64 (see CONTRACT.md "Environment").

# Runtime — Postgres access and schema migrations.
psycopg[binary]==3.3.4
SQLAlchemy==2.0.51
alembic==1.18.5

# Test
pytest==9.1.1
```

Create the venv and install:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

Edit `.gitignore` — add two lines (keep the existing ones):

```text
config.toml
.gh_token
.env
state/
logs/
repos/
worktrees/
__pycache__/
*.pyc
.venv/
state/journal.jsonl
```

`state/` already matches `state/journal.jsonl`, but the journal is called out
explicitly because from Task 11 onward it is operationally significant (it is
what a crashed harness replays on restart), unlike the rest of `state/`.

Edit `README.md` — replace the zero-dependency claim in Prerequisites and Setup:

```markdown
## Prerequisites

- **Python 3.11+** (development uses **3.14**)
- **Git** on PATH
- **GitHub CLI (`gh`)** on PATH, authenticated (`gh auth status` should show logged in)
- **Claude CLI (`claude`)** on PATH, authenticated
- **PostgreSQL** reachable via `PIPELINE_METRICS_DATABASE_URL` (optional — see
  `[database]` in `config.toml.sample`; auto-claude runs in degraded,
  local-only mode without it)
```

```markdown
3. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt   # Windows
   ```

4. Copy the sample config and edit it for your environment:

   ```bash
   cp config.toml.sample config.toml
   ```

   At minimum, set `org` and `repos` under `[github]` to match your GitHub organization. `config.toml` is gitignored — your real settings stay local.
```

Remove the line `That's it — zero dependencies to install.` — it is no longer true.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_version.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 495 passed, 0 failed (493 existing + 2 new in `test_version.py`)

- [ ] **Step 6: Commit**

```bash
git add version.py requirements.txt .gitignore README.md tests/test_version.py
git commit -m "feat(db): scaffold venv, pinned deps, and version.py (ai-cc)"
```

---

### Task 2: `DatabaseConfig` + `[database]` section

**Files:**
- Modify: `config.py`
- Modify: `config.toml.sample`
- Test: `tests/test_db_config.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `config.DatabaseConfig(enabled, url_env, lease_ttl_seconds, heartbeat_interval_seconds, journal_file, connect_timeout_seconds)` with `.url() -> str | None`; `Config.database: DatabaseConfig`. Consumed by every later task that reads `config.database.*` (Tasks 3, 11, and the Phase C/D/E lease/journal tasks).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_config.py
"""Tests for config.py's [database] section.

The real config.toml on disk today has no [database] block at all, so
load_config must treat its absence as "use every default" rather than raising
— the existing `raw["github"]`-style splat would turn a missing block into a
TypeError the moment DatabaseConfig gained a single required field. This also
covers DatabaseConfig.url(), which reads the environment lazily so a value
set by main's .env loading (which runs before load_config) is visible.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DatabaseConfig, load_config  # noqa: E402

_MINIMAL_BODY = """
[github]
org = "Accelevation"
repos = ["field_admin"]
poll_interval_seconds = 60
base_branch = "dev"
label_prefix = "ac-"
needs_info_label = "ac-needs-info"
pr_created_label = "ac-pr-created"
in_progress_label = "ac-in-progress"
action_labels = ["ac-implement"]
dev_actions = ["implement"]
rework_label = "ac-rework"

[claude]
triage_model = "claude-haiku-4-5"
dev_model = "claude-sonnet-4-6"
permission_mode = "bypassPermissions"
max_budget_usd = 10.0
output_format = "stream-json"

[workers]
max_parallel = 3
max_continuations = 2
shutdown_grace_seconds = 30

[paths]
repos_dir = "repos"
worktrees_dir = "worktrees"
state_file = "state/issues.json"
log_file = "logs/auto-claude.log"
prompts_dir = "prompts"

[logging]
level = "INFO"
colorize = true
log_to_file = true
"""


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "config.toml"
    p.write_text(_MINIMAL_BODY + extra, encoding="utf-8")
    return p


class TestDatabaseSectionAbsent:
    def test_defaults_when_database_section_missing(self, tmp_path):
        # This is the real config.toml's shape today — no [database] block.
        cfg = load_config(_write_config(tmp_path))
        assert cfg.database.enabled is True
        assert cfg.database.url_env == "PIPELINE_METRICS_DATABASE_URL"
        assert cfg.database.lease_ttl_seconds == 1800
        assert cfg.database.heartbeat_interval_seconds == 60
        assert cfg.database.connect_timeout_seconds == 10
        assert cfg.database.journal_file == tmp_path / "state" / "journal.jsonl"


class TestDatabaseSectionPresent:
    def test_overrides_are_applied(self, tmp_path):
        extra = (
            "\n[database]\n"
            "enabled = false\n"
            "url_env = \"CUSTOM_DB_URL\"\n"
            "lease_ttl_seconds = 900\n"
            "heartbeat_interval_seconds = 30\n"
            "connect_timeout_seconds = 5\n"
        )
        cfg = load_config(_write_config(tmp_path, extra))
        assert cfg.database.enabled is False
        assert cfg.database.url_env == "CUSTOM_DB_URL"
        assert cfg.database.lease_ttl_seconds == 900
        assert cfg.database.heartbeat_interval_seconds == 30
        assert cfg.database.connect_timeout_seconds == 5

    def test_journal_file_resolved_relative_to_project_root(self, tmp_path):
        extra = '\n[database]\njournal_file = "custom/journal.jsonl"\n'
        cfg = load_config(_write_config(tmp_path, extra))
        assert cfg.database.journal_file == tmp_path / "custom" / "journal.jsonl"


class TestDatabaseConfigUrl:
    def test_url_reads_from_environment(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgresql://x/y")
        assert DatabaseConfig().url() == "postgresql://x/y"

    def test_url_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("PIPELINE_METRICS_DATABASE_URL", raising=False)
        assert DatabaseConfig().url() is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_db_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'DatabaseConfig' from 'config'`

- [ ] **Step 3: Write the implementation**

```python
# config.py — add near the other @dataclass(frozen=True) config classes,
# after IntegrationsConfig
import os as _os  # already imported as `os` at module top — no new import needed


@dataclass(frozen=True)
class DatabaseConfig:
    """Optional Postgres-backed shared state (docs/plans/
    12-shared-state-in-postgres.md). Has a default so `[database]` being
    absent from config.toml — true of the current file — and Config being
    constructed positionally in existing tests both keep working.
    """

    enabled: bool = True
    url_env: str = "PIPELINE_METRICS_DATABASE_URL"
    lease_ttl_seconds: int = 1800
    heartbeat_interval_seconds: int = 60
    journal_file: Path = Path("state/journal.jsonl")
    connect_timeout_seconds: int = 10

    def url(self) -> str | None:
        """The connection string, read lazily so a value main() places in
        os.environ from .env before load_config() is visible. Never logged —
        callers must not put this in a log line."""
        return os.environ.get(self.url_env) or None
```

```python
# config.py — Config dataclass: add a field with a default (order matters —
# it must come after every field without one)
@dataclass(frozen=True)
class Config:
    github: GithubConfig
    claude: ClaudeConfig
    workers: WorkersConfig
    paths: PathsConfig
    logging: LoggingConfig
    integrations: IntegrationsConfig
    repo_setup: dict[str, "RepoSetupConfig"] = dataclass_field(default_factory=dict)
    database: DatabaseConfig = dataclass_field(default_factory=DatabaseConfig)
```

```python
# config.py — inside load_config(), after the `integrations = IntegrationsConfig(...)`
# block and before the `repo_setup` loop. Uses raw.get(...), NOT the
# **raw["database"] splat load_config uses for [github]/[workers]/[logging] —
# those sections are required and their absence should be a loud TypeError;
# [database] is optional by design and must not raise merely for being absent.
    database_raw = raw.get("database", {})
    _db_defaults = DatabaseConfig()
    database = DatabaseConfig(
        enabled=database_raw.get("enabled", _db_defaults.enabled),
        url_env=database_raw.get("url_env", _db_defaults.url_env),
        lease_ttl_seconds=database_raw.get(
            "lease_ttl_seconds", _db_defaults.lease_ttl_seconds
        ),
        heartbeat_interval_seconds=database_raw.get(
            "heartbeat_interval_seconds", _db_defaults.heartbeat_interval_seconds
        ),
        journal_file=_resolve_path(
            project_root,
            database_raw.get("journal_file", str(_db_defaults.journal_file)),
        ),
        connect_timeout_seconds=database_raw.get(
            "connect_timeout_seconds", _db_defaults.connect_timeout_seconds
        ),
    )
```

```python
# config.py — the final `return Config(...)` call gains one line
    return Config(
        repo_setup=repo_setup,
        github=GithubConfig(**raw["github"]),
        claude=claude,
        workers=WorkersConfig(**raw["workers"]),
        paths=paths,
        logging=LoggingConfig(**raw["logging"]),
        integrations=integrations,
        database=database,
    )
```

Edit `config.toml.sample` — append after `[integrations]`'s block:

```toml
# Optional. Shares issue state, lease and run history across harnesses via the
# `auto_claude` schema on PIPELINE_METRICS_DATABASE_URL (see docs/plans/
# 12-shared-state-in-postgres.md). Omit this section entirely to use every
# default below. State stays purely local — degraded, not dead — whenever the
# url_env variable is unset, regardless of `enabled`.
[database]
enabled = true
url_env = "PIPELINE_METRICS_DATABASE_URL"
lease_ttl_seconds = 1800
heartbeat_interval_seconds = 60
journal_file = "state/journal.jsonl"
connect_timeout_seconds = 10
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_db_config.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 500 passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add config.py config.toml.sample tests/test_db_config.py
git commit -m "feat(db): add DatabaseConfig and the optional [database] section (ai-cc)"
```

---

### Task 3: `db/pool.py` — `Database`, `DbUnavailable`

**Files:**
- Create: `db/__init__.py`
- Create: `db/pool.py`
- Test: `tests/test_db_pool.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `db.pool.Database(url, *, connect=None, connect_timeout=10, retries=2, sleep=time.sleep)`, `.execute(sql, params=()) -> list[tuple]`, `.close()`; `db.pool.DbUnavailable(RuntimeError)`. Consumed by every remaining db/ module and by `main.py` (Task 5, Task 11).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_pool.py
"""Tests for db/pool.py — Database's retry-then-fail and reconnect behavior.

No real Postgres involved anywhere in this file: `connect` is injected, so
retry and reconnect logic is exercised entirely with fakes, per the house
rule ("No test touches a real DB, network or subprocess", CONTRACT.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.pool import Database, DbUnavailable  # noqa: E402


class FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows
        self.description = description

    def execute(self, sql, params):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """`fail_times` cursor() calls raise OperationalError before succeeding —
    simulates a connection that answers but whose query keeps resetting."""

    def __init__(self, rows=(), description=(("col",),), fail_times=0):
        self.closed = False
        self._rows = rows
        self._description = description
        self._fail_times = fail_times
        self._calls = 0

    def cursor(self):
        self._calls += 1
        if self._calls <= self._fail_times:
            raise psycopg.OperationalError("connection reset")
        return FakeCursor(self._rows, self._description)

    def close(self):
        self.closed = True


def _connect_returning(conn):
    def _connect(url, **kwargs):
        return conn
    return _connect


class TestDatabaseExecute:
    def test_returns_rows_from_a_select(self):
        conn = FakeConnection(rows=[(1, "a"), (2, "b")])
        db = Database("postgresql://x", connect=_connect_returning(conn))
        assert db.execute("SELECT * FROM t") == [(1, "a"), (2, "b")]

    def test_returns_empty_list_for_a_statement_with_no_result_set(self):
        conn = FakeConnection(rows=[], description=None)
        db = Database("postgresql://x", connect=_connect_returning(conn))
        assert db.execute("UPDATE t SET x = 1") == []


class TestDatabaseRetries:
    def test_retries_on_operational_error_then_succeeds(self):
        conn = FakeConnection(rows=[(1,)], fail_times=1)
        sleeps = []
        db = Database("postgresql://x", connect=_connect_returning(conn),
                       retries=2, sleep=lambda s: sleeps.append(s))
        assert db.execute("SELECT 1") == [(1,)]
        assert sleeps, "must sleep between retries"

    def test_raises_db_unavailable_after_exhausting_retries(self):
        conn = FakeConnection(fail_times=999)
        db = Database("postgresql://x", connect=_connect_returning(conn),
                       retries=2, sleep=lambda s: None)
        with pytest.raises(DbUnavailable):
            db.execute("SELECT 1")


class TestDatabaseReconnects:
    def test_reconnects_after_a_dropped_connection(self):
        conns = [FakeConnection(rows=[(1,)]), FakeConnection(rows=[(2,)])]

        def connect(url, **kwargs):
            return conns.pop(0)

        db = Database("postgresql://x", connect=connect)
        assert db.execute("SELECT 1") == [(1,)]

        # Simulate the pooler recycling the connection between calls —
        # Database must notice `.closed` and open a new one rather than
        # reusing a dead socket.
        db._conn.closed = True

        assert db.execute("SELECT 1") == [(2,)]


class TestDatabaseClose:
    def test_close_before_any_connection_is_a_safe_noop(self):
        db = Database("postgresql://x", connect=_connect_returning(FakeConnection()))
        db.close()
        assert db._conn is None

    def test_close_after_use_marks_the_connection_closed(self):
        conn = FakeConnection(rows=[(1,)])
        db = Database("postgresql://x", connect=_connect_returning(conn))
        db.execute("SELECT 1")
        db.close()
        assert conn.closed
        assert db._conn is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_db_pool.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: Write the implementation**

```python
# db/__init__.py
"""Postgres-backed shared state for auto-claude (docs/plans/
12-shared-state-in-postgres.md). Runtime SQL is raw psycopg3 throughout —
SQLAlchemy exists only because Alembic requires it (migrations/env.py).
"""
```

```python
# db/pool.py
"""db/pool.py — a small, retrying Postgres connection wrapper.

`Database` owns exactly one lazily-opened connection, reused across calls,
and re-opened whenever it is found closed (dropped by the pooler, or by a
prior OperationalError). Autocommit: every write here is either a single
atomic statement (the lease claim) or safely re-runnable (ON CONFLICT
upserts), so there is nothing a transaction would buy that idempotency does
not already provide.
"""

from __future__ import annotations

import time
from typing import Callable

import psycopg


class DbUnavailable(RuntimeError):
    """Postgres could not be reached. Never raised for SQL/logic errors —
    those propagate as whatever psycopg/Postgres raised."""


class Database:
    def __init__(
        self,
        url: str,
        *,
        connect: Callable[..., "psycopg.Connection"] | None = None,
        connect_timeout: int = 10,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = url
        self._connect = connect or psycopg.connect
        self._connect_timeout = connect_timeout
        self._retries = retries
        self._sleep = sleep
        self._conn = None

    def _ensure_connected(self):
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = self._connect(
            self._url, connect_timeout=self._connect_timeout, autocommit=True
        )
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Autocommit. Returns [] for statements with no result set.
        Retries `retries` times on OperationalError, then raises DbUnavailable."""
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                conn = self._ensure_connected()
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    if cur.description is None:
                        return []
                    return cur.fetchall()
            except psycopg.OperationalError as exc:
                last_exc = exc
                # The connection is presumed dead — drop it so the next
                # attempt opens a fresh one instead of retrying a socket that
                # will never recover.
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                if attempt < self._retries:
                    self._sleep(2 ** attempt)
        raise DbUnavailable(
            f"could not reach Postgres after {self._retries + 1} attempt(s): {last_exc}"
        ) from last_exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_db_pool.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 507 passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add db/__init__.py db/pool.py tests/test_db_pool.py
git commit -m "feat(db): add Database — a retrying, reconnecting psycopg3 wrapper (ai-cc)" 
```

---

### Task 4: Alembic scaffold — `auto_claude` schema

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_initial.py`
- Test: `tests/test_migration_0001.py`

**Interfaces:**
- Consumes: `PIPELINE_METRICS_DATABASE_URL` env var (real-DB verification step only)
- Produces: the `auto_claude` schema and its four tables exactly as specified in `CONTRACT.md`'s SQL block; `auto_claude.alembic_version` at revision `"0001_initial"`. Consumed by `db/schema.py` (Task 5, `EXPECTED_REVISION = "0001_initial"`) and every other `db/*.py` module.

This is the one task in Phase A/B whose real verification runs against the
live Supabase database from `PIPELINE_METRICS_DATABASE_URL`, per
`CONTRACT.md` — everything else in this plan uses fakes only.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_0001.py
"""Structural checks on the Alembic scaffold.

No real database here — that is Step 6 below, run once by hand against the
live Supabase instance, not on every `pytest` run. This file guards the one
trap docs/plans/12-shared-state-in-postgres.md calls out by name:
`version_table_schema="auto_claude"` means Alembic reads its version table out
of a schema that must already exist, so `CREATE SCHEMA IF NOT EXISTS
auto_claude` has to run in migrations/env.py *before* `context.configure(...)`
executes — not inside revision 0001, where it would be too late for
`context.configure` itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


class TestEnvCreatesSchemaBeforeConfigure:
    def test_create_schema_appears_before_context_configure(self):
        text = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        create_at = text.index("CREATE SCHEMA IF NOT EXISTS auto_claude")
        configure_at = text.index("context.configure(")
        assert create_at < configure_at, (
            "schema must be created before context.configure() reads the "
            "version table out of it"
        )

    def test_version_table_schema_is_auto_claude(self):
        text = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        assert 'version_table_schema="auto_claude"' in text


class TestRevision0001CreatesTheFrozenSchema:
    def test_creates_all_four_tables(self):
        text = (ROOT / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
        for table in ("harness", "issue_state", "run", "summary"):
            assert f"CREATE TABLE auto_claude.{table}" in text

    def test_lease_columns_have_no_default(self):
        # Guards Task 7's "upsert never touches the lease columns" rule one
        # layer down — a DEFAULT here would make it easy for a future INSERT
        # to accidentally seed a lease value.
        text = (ROOT / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("owner_harness_id", "lease_expires_at", "heartbeat_at")):
                assert "DEFAULT" not in stripped, f"lease column must have no default: {stripped!r}"

    def test_down_revision_is_none_and_id_matches_filename(self):
        text = (ROOT / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
        assert 'revision: str = "0001_initial"' in text
        assert "down_revision: Union[str, None] = None" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_migration_0001.py -v`
Expected: FAIL with `FileNotFoundError` (no `migrations/` directory yet)

- [ ] **Step 3: Write the implementation**

```ini
# alembic.ini
[alembic]
script_location = migrations
prepend_sys_path = .
version_path_separator = os

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

```python
# migrations/env.py
"""Alembic environment for auto-claude's `auto_claude` Postgres schema.

Two things this file must get right (docs/plans/12-shared-state-in-postgres.md,
"Migrations"):

1. `version_table_schema="auto_claude"` keeps Alembic's own bookkeeping out of
   `public`, where the Node toolchain's `pipeline_events` /
   `pipeline_schema_migrations` already live — the two migration systems must
   never collide.
2. That schema has to exist *before* `context.configure()` runs, because
   Alembic tries to read the version table out of it immediately. Revision
   0001 also issues `CREATE SCHEMA IF NOT EXISTS auto_claude`, but by the time
   0001 runs, `context.configure()` has already needed the schema to exist —
   so the CREATE here is not redundant, it is what makes the very first
   `alembic upgrade head` possible at all.

Connection string: PIPELINE_METRICS_DATABASE_URL — the same variable
`integrations.py` and `config.DatabaseConfig.url_env`'s default read. Moving
that database moves auto-claude's operational state with it.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    url = os.environ.get("PIPELINE_METRICS_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "PIPELINE_METRICS_DATABASE_URL is not set — Alembic needs it to "
            "reach the database. Set it in .env or the environment."
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        version_table_schema="auto_claude",
        include_schemas=True,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    with engine.connect() as connection:
        # Must run before context.configure(): configure() immediately tries
        # to read the version table out of `auto_claude`, which does not
        # exist yet on a virgin database.
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS auto_claude"))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema="auto_claude",
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

```mako
## migrations/script.py.mako — Alembic's stock revision template, unmodified.
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

```python
# migrations/versions/0001_initial.py
"""initial auto_claude schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-29

Creates the `auto_claude` schema and its four tables exactly as frozen in
docs/plans/12-shared-state-in-postgres.md and CONTRACT.md. Raw DDL via
`op.execute` — no SQLAlchemy Table objects. Alembic is the only consumer in
this project that needs SQLAlchemy at all; everything else is raw psycopg3.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS auto_claude")

    op.execute("""
        CREATE TABLE auto_claude.harness (
            id           text PRIMARY KEY,
            hostname     text NOT NULL,
            pid          integer NOT NULL,
            version      text NOT NULL,
            started_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE auto_claude.issue_state (
            issue_id            text PRIMARY KEY,
            repo                text NOT NULL,
            number              integer NOT NULL,
            title               text NOT NULL DEFAULT '',
            stage               text,
            kind                text,
            mode                text NOT NULL DEFAULT 'dev',
            branch              text,
            pr_url              text,
            triage_attempts     integer NOT NULL DEFAULT 0,
            rework_count        integer NOT NULL DEFAULT 0,
            continuation_count  integer NOT NULL DEFAULT 0,
            last_error          text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            owner_harness_id    text REFERENCES auto_claude.harness(id) ON DELETE SET NULL,
            lease_expires_at    timestamptz,
            heartbeat_at        timestamptz
        )
    """)
    op.execute(
        "CREATE INDEX issue_state_lease_idx "
        "ON auto_claude.issue_state (owner_harness_id, lease_expires_at)"
    )

    op.execute("""
        CREATE TABLE auto_claude.run (
            id               text PRIMARY KEY,
            issue_id         text NOT NULL REFERENCES auto_claude.issue_state(issue_id) ON DELETE CASCADE,
            harness_id       text REFERENCES auto_claude.harness(id) ON DELETE SET NULL,
            mode             text NOT NULL,
            model            text,
            started_at       timestamptz NOT NULL DEFAULT now(),
            ended_at         timestamptz,
            outcome          text,
            exit_code        integer,
            duration_seconds integer,
            cost_usd         numeric(10,4),
            turns            integer,
            crash_log_path   text
        )
    """)
    op.execute(
        "CREATE INDEX run_issue_idx ON auto_claude.run (issue_id, started_at DESC)"
    )

    op.execute("""
        CREATE TABLE auto_claude.summary (
            id          text PRIMARY KEY,
            issue_id    text NOT NULL REFERENCES auto_claude.issue_state(issue_id) ON DELETE CASCADE,
            run_id      text REFERENCES auto_claude.run(id) ON DELETE SET NULL,
            kind        text NOT NULL,
            body        text NOT NULL,
            comment_url text,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX summary_issue_idx ON auto_claude.summary (issue_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA auto_claude CASCADE")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_migration_0001.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 512 passed, 0 failed

- [ ] **Step 6: Verify against the real database (the exception to "no test touches a real DB")**

```powershell
$env:PIPELINE_METRICS_DATABASE_URL = "<the Supabase session-pooler URL, port 5432>"
.venv\Scripts\python -m alembic upgrade head
```
Expected: no errors; final log line shows `Running upgrade  -> 0001_initial`.

```powershell
.venv\Scripts\python -c "
import os, psycopg
conn = psycopg.connect(os.environ['PIPELINE_METRICS_DATABASE_URL'])
rows = conn.execute(
    \"SELECT table_name FROM information_schema.tables WHERE table_schema = 'auto_claude' ORDER BY table_name\"
).fetchall()
print(rows)
"
```
Expected: `[('alembic_version',), ('harness',), ('issue_state',), ('run',), ('summary',)]`

```powershell
.venv\Scripts\python -m alembic downgrade base
.venv\Scripts\python -m alembic upgrade head
```
Expected: both succeed with no errors — proves the migration round-trips
cleanly, not just that it ran once.

- [ ] **Step 7: Commit**

```bash
git add alembic.ini migrations/ tests/test_migration_0001.py
git commit -m "feat(db): add Alembic scaffold creating the auto_claude schema (ai-cc)"
```

---

### Task 5: `db/schema.py` — schema-currency gate, wired into `main()`

**Files:**
- Create: `db/schema.py`
- Modify: `main.py` (new helper `_check_schema_gate`, placed near `_abort` around line 89)
- Modify: `tests/test_wiring.py`
- Test: `tests/test_db_schema.py`

**Interfaces:**
- Consumes: `db.pool.Database`, `db.pool.DbUnavailable` (Task 3)
- Produces: `db.schema.EXPECTED_REVISION: str`, `db.schema.SchemaOutOfDate(RuntimeError)`, `db.schema.current_revision(db) -> str | None`, `db.schema.check_schema_current(db) -> None`; `main._check_schema_gate(db, logger) -> None`. Consumed by Task 11's `_init_db_layer`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_schema.py
"""Tests for db/schema.py — the daemon never migrates; it only checks.

docs/plans/12-shared-state-in-postgres.md, "Migrations": "The daemon never
migrates. It checks at startup whether the schema is current; if it is
behind, it refuses to start and prints the command to run." All of `db.schema`
is tested against a FakeDatabase — no real Postgres, matching every other
db/*.py test in this plan except Task 4's migration round-trip.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.pool import DbUnavailable  # noqa: E402
from db.schema import (  # noqa: E402
    EXPECTED_REVISION,
    SchemaOutOfDate,
    check_schema_current,
    current_revision,
)


class FakeDatabase:
    def __init__(self, rows=None, raises=None):
        self._rows = rows
        self._raises = raises

    def execute(self, sql, params=()):
        if self._raises is not None:
            raise self._raises
        return self._rows if self._rows is not None else []


class TestCurrentRevision:
    def test_returns_the_revision_when_the_table_exists(self):
        db = FakeDatabase(rows=[(EXPECTED_REVISION,)])
        assert current_revision(db) == EXPECTED_REVISION

    def test_returns_none_when_the_table_does_not_exist(self):
        db = FakeDatabase(raises=RuntimeError(
            'relation "auto_claude.alembic_version" does not exist'))
        assert current_revision(db) is None

    def test_propagates_db_unavailable_rather_than_treating_it_as_missing(self):
        db = FakeDatabase(raises=DbUnavailable("Postgres is down"))
        with pytest.raises(DbUnavailable):
            current_revision(db)


class TestCheckSchemaCurrent:
    def test_passes_silently_when_current(self):
        db = FakeDatabase(rows=[(EXPECTED_REVISION,)])
        check_schema_current(db)  # must not raise

    def test_raises_schema_out_of_date_with_the_upgrade_command_when_behind(self):
        db = FakeDatabase(rows=[("0000_before",)])
        with pytest.raises(SchemaOutOfDate) as excinfo:
            check_schema_current(db)
        assert "alembic upgrade head" in str(excinfo.value)

    def test_raises_when_schema_is_missing_entirely(self):
        db = FakeDatabase(raises=RuntimeError('relation "..." does not exist'))
        with pytest.raises(SchemaOutOfDate) as excinfo:
            check_schema_current(db)
        assert "None" in str(excinfo.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_db_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.schema'`

- [ ] **Step 3: Write the implementation**

```python
# db/schema.py
"""db/schema.py — the schema-currency gate.

The daemon never migrates (docs/plans/12-shared-state-in-postgres.md,
"Migrations"): it checks at startup whether `auto_claude.alembic_version`
matches EXPECTED_REVISION and refuses to start otherwise, printing the exact
`alembic upgrade head` command rather than running it itself.
"""

from __future__ import annotations

from db.pool import Database, DbUnavailable

EXPECTED_REVISION = "0001_initial"


class SchemaOutOfDate(RuntimeError):
    """The connected database's auto_claude schema is behind EXPECTED_REVISION."""


def current_revision(db: Database) -> str | None:
    """Reads auto_claude.alembic_version. None if the table does not exist."""
    try:
        rows = db.execute("SELECT version_num FROM auto_claude.alembic_version")
    except DbUnavailable:
        raise
    except Exception:
        # UndefinedTable (or any other "the table isn't there yet") means the
        # very first migration has never run — a legitimate "None", not an
        # unhandled traceback, so a virgin database gets the SchemaOutOfDate
        # message rather than a crash.
        return None
    if not rows:
        return None
    return rows[0][0]


def check_schema_current(db: Database) -> None:
    """Raises SchemaOutOfDate with the exact command to run, if behind."""
    actual = current_revision(db)
    if actual == EXPECTED_REVISION:
        return
    raise SchemaOutOfDate(
        f"auto_claude schema is at {actual!r}, expected {EXPECTED_REVISION!r}. "
        f"Run: .venv\\Scripts\\python -m alembic upgrade head"
    )
```

```python
# main.py — add near `_abort` (around line 89), after its definition
def _check_schema_gate(db, logger: MainLogger) -> None:
    """Refuse to start against a Postgres schema behind EXPECTED_REVISION.
    The daemon never migrates itself — see db/schema.py and docs/plans/
    12-shared-state-in-postgres.md, "Migrations"."""
    try:
        check_schema_current(db)
    except SchemaOutOfDate as exc:
        _abort(logger, str(exc))
    except DbUnavailable as exc:
        _abort(logger, f"Cannot reach Postgres to verify the schema: {exc}")
```

```python
# main.py — add to the import block near the top, alongside the existing
# `from state import IssueStatus, StateStore`
from db.pool import DbUnavailable
from db.schema import SchemaOutOfDate, check_schema_current
```

```python
# tests/test_wiring.py — add to the import block at the top
from db.schema import SchemaOutOfDate  # noqa: E402
```

```python
# tests/test_wiring.py — new class, anywhere after the existing test classes
class TestSchemaGate:
    def test_aborts_when_schema_is_out_of_date(self, monkeypatch):
        monkeypatch.setattr(
            main, "check_schema_current",
            lambda _db: (_ for _ in ()).throw(SchemaOutOfDate("run: alembic upgrade head")),
        )
        with pytest.raises(SystemExit) as excinfo:
            main._check_schema_gate(object(), _logger())
        assert excinfo.value.code == 1

    def test_does_nothing_when_schema_is_current(self, monkeypatch):
        monkeypatch.setattr(main, "check_schema_current", lambda _db: None)
        main._check_schema_gate(object(), _logger())  # must not raise
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_db_schema.py tests/test_wiring.py -v`
Expected: PASS (6 + 2 new tests; existing `test_wiring.py` tests unaffected)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 520 passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add db/schema.py main.py tests/test_db_schema.py tests/test_wiring.py
git commit -m "feat(db): refuse startup on a stale schema instead of migrating (ai-cc)"
```

---

## Phase B — issue_state + reconciliation

### Task 6: `db/harness.py` — `Harness`, `new_harness`, `register`, `touch`

**Files:**
- Create: `db/harness.py`
- Test: `tests/test_db_harness.py`

**Interfaces:**
- Consumes: `db.pool.Database` (Task 3)
- Produces: `db.harness.Harness(id, hostname, pid, version)` (frozen dataclass), `new_harness(version) -> Harness`, `register(db, harness) -> None`, `touch(db, harness_id) -> None`. Consumed by Task 11 (`main._init_db_layer`) and by the Phase C/D/E lease/run tasks (`harness_id` foreign keys).

> `CONTRACT.md`'s "Tests" list does not name a file for this module (it lists
> one file per `db/*.py` module built in later phases, and harness.py isn't
> among them). `tests/test_db_harness.py` follows the same naming convention
> as every other `db/*.py` test file in this plan.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_harness.py
"""Tests for db/harness.py — the identity row every lease, run and summary
foreign-keys to. `register` must be an upsert, not a bare INSERT: the same
process id can legitimately reappear (a restart on the same box reuses a
recycled PID), and register() runs on every startup, not just the first.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.harness import Harness, new_harness, register, touch  # noqa: E402


class FakeDatabase:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return []


class TestNewHarness:
    def test_generates_a_fresh_uuid4_hex_id_each_call(self):
        h1 = new_harness("0.2.0")
        h2 = new_harness("0.2.0")
        assert h1.id != h2.id
        assert len(h1.id) == 32  # uuid4().hex has no dashes

    def test_captures_hostname_pid_and_version(self):
        h = new_harness("0.2.0")
        assert h.hostname == socket.gethostname()
        assert h.pid == os.getpid()
        assert h.version == "0.2.0"


class TestRegister:
    def test_inserts_with_all_four_fields(self):
        db = FakeDatabase()
        h = Harness(id="abc123", hostname="box1", pid=42, version="0.2.0")
        register(db, h)
        sql, params = db.calls[0]
        assert "INSERT INTO auto_claude.harness" in sql
        assert params == ("abc123", "box1", 42, "0.2.0")

    def test_upserts_on_conflict_rather_than_failing_on_restart(self):
        db = FakeDatabase()
        register(db, Harness(id="abc123", hostname="box1", pid=42, version="0.2.0"))
        assert "ON CONFLICT (id) DO UPDATE" in db.calls[0][0]


class TestTouch:
    def test_updates_last_seen_at_for_the_given_id(self):
        db = FakeDatabase()
        touch(db, "abc123")
        sql, params = db.calls[0]
        assert "UPDATE auto_claude.harness" in sql
        assert params == ("abc123",)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_db_harness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.harness'`

- [ ] **Step 3: Write the implementation**

```python
# db/harness.py
"""db/harness.py — this process's identity row in auto_claude.harness.

Every lease and every run/summary row references a harness id, so `register`
succeeding (or being journaled — Task 21, once a real journal exists; logged
and dropped in the meantime, Task 11) comes before anything else in db/
does useful work.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass

from db.pool import Database


@dataclass(frozen=True)
class Harness:
    id: str
    hostname: str
    pid: int
    version: str


def new_harness(version: str) -> Harness:
    return Harness(
        id=uuid.uuid4().hex,
        hostname=socket.gethostname(),
        pid=os.getpid(),
        version=version,
    )


def register(db: Database, harness: Harness) -> None:
    db.execute(
        """
        INSERT INTO auto_claude.harness (id, hostname, pid, version)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
           SET hostname = EXCLUDED.hostname,
               pid = EXCLUDED.pid,
               version = EXCLUDED.version,
               last_seen_at = now()
        """,
        (harness.id, harness.hostname, harness.pid, harness.version),
    )


def touch(db: Database, harness_id: str) -> None:
    db.execute(
        "UPDATE auto_claude.harness SET last_seen_at = now() WHERE id = %s",
        (harness_id,),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_db_harness.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 525 passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add db/harness.py tests/test_db_harness.py
git commit -m "feat(db): add db/harness.py — register and touch a harness identity (ai-cc)"
```

---

### Task 7: `db/issue_state.py` — `upsert`, `fetch`, `fetch_all`

**Files:**
- Create: `db/issue_state.py`
- Test: `tests/test_db_issue_state.py`

**Interfaces:**
- Consumes: `db.pool.Database` (Task 3)
- Produces: `db.issue_state.upsert(db, *, issue_id, repo, number, title, stage, kind, mode, branch, pr_url, triage_attempts, rework_count, continuation_count, last_error) -> None`, `fetch(db, issue_id) -> dict | None`, `fetch_all(db) -> dict[str, dict]`. Consumed by Task 10 (`reconcile`, via `db_rows`), Task 11 (`main._reconcile_at_startup`), and Task 8's `dbsync.DbSync.upsert_issue`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_issue_state.py
"""Tests for db/issue_state.py.

The one rule that matters here is structural, not behavioral: `upsert` must
NEVER reference owner_harness_id / lease_expires_at / heartbeat_at, in either
the INSERT column list or the ON CONFLICT SET clause. A state write silently
clearing or stealing a lease is exactly the bug class docs/plans/
12-shared-state-in-postgres.md design decision #2 exists to rule out by
construction — lease columns are exclusively db/lease.py's job.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.issue_state import fetch, fetch_all, upsert  # noqa: E402

_LEASE_COLUMNS = ("owner_harness_id", "lease_expires_at", "heartbeat_at")


class FakeDatabase:
    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self._rows


def _upsert(db, **overrides):
    kwargs = dict(
        issue_id="r#1", repo="r", number=1, title="t", stage="ac-dev-ready",
        kind="fix", mode="dev", branch=None, pr_url=None,
        triage_attempts=0, rework_count=0, continuation_count=0, last_error=None,
    )
    kwargs.update(overrides)
    upsert(db, **kwargs)


class TestUpsertNeverTouchesLeaseColumns:
    def test_lease_columns_are_absent_from_the_insert_column_list(self):
        db = FakeDatabase()
        _upsert(db)
        sql, _params = db.calls[0]
        insert_clause = sql[: sql.index("VALUES")]
        for column in _LEASE_COLUMNS:
            assert column not in insert_clause

    def test_lease_columns_are_absent_from_the_on_conflict_set_clause(self):
        db = FakeDatabase()
        _upsert(db)
        sql, _params = db.calls[0]
        set_clause = sql[sql.index("DO UPDATE"):]
        for column in _LEASE_COLUMNS:
            assert column not in set_clause

    def test_passes_every_non_lease_field_as_a_parameter_in_order(self):
        db = FakeDatabase()
        _upsert(db, issue_id="r#1", repo="repo", number=7, title="Title",
                stage="ac-in-progress", kind="fix", mode="dev", branch="b",
                pr_url="https://x/pull/1", triage_attempts=1, rework_count=2,
                continuation_count=3, last_error="boom")
        _sql, params = db.calls[0]
        assert params == ("r#1", "repo", 7, "Title", "ac-in-progress", "fix",
                           "dev", "b", "https://x/pull/1", 1, 2, 3, "boom")


class TestFetch:
    def test_returns_none_when_no_row(self):
        db = FakeDatabase(rows=[])
        assert fetch(db, "r#1") is None

    def test_zips_columns_onto_the_returned_row(self):
        row = ("r#1", "repo", 1, "t", "ac-dev-ready", "fix", "dev", None, None,
               0, 0, 0, None, "2026-01-01", "2026-01-01", None, None, None)
        db = FakeDatabase(rows=[row])
        result = fetch(db, "r#1")
        assert result["issue_id"] == "r#1"
        assert result["stage"] == "ac-dev-ready"
        assert result["owner_harness_id"] is None


class TestFetchAll:
    def test_keys_the_result_by_issue_id(self):
        row1 = ("r#1", "repo", 1, "t1", None, None, "dev", None, None, 0, 0, 0,
                None, "x", "x", None, None, None)
        row2 = ("r#2", "repo", 2, "t2", None, None, "dev", None, None, 0, 0, 0,
                None, "x", "x", "harness-a", "2099-01-01", "2026-01-01")
        db = FakeDatabase(rows=[row1, row2])
        result = fetch_all(db)
        assert set(result) == {"r#1", "r#2"}
        assert result["r#2"]["owner_harness_id"] == "harness-a"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_db_issue_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.issue_state'`

- [ ] **Step 3: Write the implementation**

```python
# db/issue_state.py
"""db/issue_state.py — the mutable-counters half of an issue's Postgres row.

`upsert` NEVER touches owner_harness_id / lease_expires_at / heartbeat_at —
not an oversight to fix later, but what keeps a state write (a counter bump,
a branch name landing) from ever silently stealing or clearing a lease
another harness holds. Those three columns are exclusively db/lease.py's job.
"""

from __future__ import annotations

from db.pool import Database

_COLUMNS = (
    "issue_id", "repo", "number", "title", "stage", "kind", "mode",
    "branch", "pr_url", "triage_attempts", "rework_count",
    "continuation_count", "last_error", "created_at", "updated_at",
    "owner_harness_id", "lease_expires_at", "heartbeat_at",
)


def upsert(db: Database, *, issue_id: str, repo: str, number: int, title: str,
           stage: str | None, kind: str | None, mode: str,
           branch: str | None, pr_url: str | None,
           triage_attempts: int, rework_count: int, continuation_count: int,
           last_error: str | None) -> None:
    """INSERT ... ON CONFLICT (issue_id) DO UPDATE. Never touches lease columns."""
    db.execute(
        """
        INSERT INTO auto_claude.issue_state
            (issue_id, repo, number, title, stage, kind, mode,
             branch, pr_url, triage_attempts, rework_count,
             continuation_count, last_error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (issue_id) DO UPDATE
           SET repo = EXCLUDED.repo,
               number = EXCLUDED.number,
               title = EXCLUDED.title,
               stage = EXCLUDED.stage,
               kind = EXCLUDED.kind,
               mode = EXCLUDED.mode,
               branch = EXCLUDED.branch,
               pr_url = EXCLUDED.pr_url,
               triage_attempts = EXCLUDED.triage_attempts,
               rework_count = EXCLUDED.rework_count,
               continuation_count = EXCLUDED.continuation_count,
               last_error = EXCLUDED.last_error,
               updated_at = now()
        """,
        (issue_id, repo, number, title, stage, kind, mode, branch, pr_url,
         triage_attempts, rework_count, continuation_count, last_error),
    )


def fetch(db: Database, issue_id: str) -> dict | None:
    rows = db.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM auto_claude.issue_state WHERE issue_id = %s",
        (issue_id,),
    )
    if not rows:
        return None
    return dict(zip(_COLUMNS, rows[0]))


def fetch_all(db: Database) -> dict[str, dict]:
    """issue_id -> row dict."""
    rows = db.execute(f"SELECT {', '.join(_COLUMNS)} FROM auto_claude.issue_state")
    return {row[0]: dict(zip(_COLUMNS, row)) for row in rows}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_db_issue_state.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 531 passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add db/issue_state.py tests/test_db_issue_state.py
git commit -m "feat(db): add db/issue_state.py — upsert/fetch that never touch a lease (ai-cc)"
```

---

### Task 8: `dbsync.py` — the write seam

**Files:**
- Create: `dbsync.py`
- Test: `tests/test_dbsync.py`

**Interfaces:**
- Consumes: `db.pool.Database`, `db.pool.DbUnavailable` (Task 3); `db.harness.Harness` (Task 6); `db.issue_state.upsert` (Task 7); `state.IssueRecord` (existing).
- Produces: `DbSync` — the single seam `main`, `process_manager` and `worker` use to reach Postgres, introduced here with only the methods whose backing module already exists and grown module-by-module by later tasks (see "Sequencing and the `dbsync` dependency" above). At this task:
  ```python
  class DbSync:
      def __init__(self, db: Database | None, harness: Harness, logger, *,
                   journal: Journal | None = None, ttl_seconds: int = 1800) -> None: ...

      @property
      def enabled(self) -> bool: ...        # False when db is None

      def upsert_issue(self, record: IssueRecord, stage: str | None) -> None: ...
  ```
  `journal` and `ttl_seconds` are accepted now, stored, and otherwise unused — `journal` so Task 21 can wire a real `db.journal.Journal` in without ever changing this signature again (this is the one deliberate deviation from `CONTRACT.md`'s positional `(db, journal, harness, logger)` order, authorised specifically for this reason); `ttl_seconds` so Task 13's lease methods have somewhere to read a configured TTL from. Consumed by Task 11 (`main._init_db_layer` constructs it), Task 13 (adds the lease methods), Task 18 (`start_run`/`finish_run`), Task 19 (`add_summary`), and Task 21 (upgrades the failure sink to a real journal).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dbsync.py
"""Tests for dbsync.py — the seam main/process_manager/worker read and write
Postgres through. This task lands before db/lease.py, db/history.py or
db/journal.py exist (see the plan's "Sequencing and the dbsync dependency"
table), so only `enabled` and `upsert_issue` exist yet; a durable write that
cannot reach Postgres is logged and discarded rather than journaled — safe
because GitHub labels stay truth and startup reconciliation (Tasks 10-11)
rebuilds issues.json from scratch on every restart regardless. Task 21
upgrades this from log-and-drop to a real journal without touching any
signature here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbsync import DbSync  # noqa: E402
from db.harness import Harness  # noqa: E402
from db.pool import DbUnavailable  # noqa: E402
from state import IssueRecord  # noqa: E402


class FakeLogger:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def info(self, msg):
        self.messages.append(("info", msg))

    def warn(self, msg):
        self.messages.append(("warn", msg))

    def error(self, msg):
        self.messages.append(("error", msg))


class FakeDatabase:
    """A Database stand-in whose execute() can be told to fail."""

    def __init__(self, raises: Exception | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self._raises = raises

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if self._raises is not None:
            raise self._raises
        return []


def _make_record(**overrides):
    defaults = dict(
        issue_id="repo#1", repo="repo", number=1, title="t", body="",
        labels=[], action="implement", status="queued",
        discovered_at="", updated_at="", issue_updated_at="",
        branch=None, pr_url=None, triage_attempts=0, error=None,
        rework_count=0, continuation_count=0,
    )
    defaults.update(overrides)
    return IssueRecord(**defaults)


HARNESS = Harness(id="h1", hostname="box", pid=1, version="0.2.0")


class TestEnabled:
    def test_false_when_db_is_none(self):
        sync = DbSync(None, HARNESS, FakeLogger())
        assert sync.enabled is False

    def test_true_when_db_is_present(self):
        sync = DbSync(FakeDatabase(), HARNESS, FakeLogger())
        assert sync.enabled is True


class TestUpsertIssueNeverRaises:
    def test_succeeds_silently_when_postgres_is_reachable(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger())

        sync.upsert_issue(_make_record(), stage="ac-in-progress")

        assert db.calls, "must have issued a write, not short-circuited"

    def test_db_unavailable_is_logged_and_swallowed_not_raised(self):
        db = FakeDatabase(raises=DbUnavailable("down"))
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger)

        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise

        assert any("dropping" in msg.lower() or "unreachable" in msg.lower()
                   for _lvl, msg in logger.messages)

    def test_a_non_connectivity_error_is_also_logged_and_swallowed(self):
        # A bad payload must not crash the caller any more than a dropped
        # connection does — both are "we could not durably write this".
        db = FakeDatabase(raises=ValueError("value too long"))
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger)

        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise

        assert any("failed" in msg.lower() for _lvl, msg in logger.messages)

    def test_no_database_at_all_is_a_silent_no_op(self):
        sync = DbSync(None, HARNESS, FakeLogger())
        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_dbsync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dbsync'`

- [ ] **Step 3: Write the implementation**

```python
# dbsync.py
"""dbsync.py — the single seam `main`, `process_manager` and `worker` use to
reach Postgres.

Introduced here, at the first task that needs it, with only the methods
whose backing module already exists (`db/pool.py`, `db/harness.py`,
`db/issue_state.py`). It grows module-by-module as later tasks land — see
the plan's "Sequencing and the `dbsync` dependency" table — without any
earlier caller ever having to change how it constructs or calls this class.

At this task, a durable write that cannot reach Postgres is logged and
dropped, not journaled: `db/journal.py` does not exist until Task 20. That is
safe because GitHub labels remain truth and startup reconciliation (Tasks
10-11) rebuilds `issues.json` from scratch on every restart regardless. Task
21 upgrades this method to journal instead, without changing this
signature — `journal` is already accepted and stored for exactly that.
"""

from __future__ import annotations

from db import issue_state
from db.harness import Harness
from db.pool import Database, DbUnavailable
from state import IssueRecord


class DbSync:
    """Postgres access for the harness. At this task, durable writes that
    fail are logged and dropped; Task 21 upgrades that to a real journal."""

    def __init__(self, db: Database | None, harness: Harness, logger, *,
                 journal=None, ttl_seconds: int = 1800) -> None:
        self._db = db
        self._harness = harness
        self._logger = logger
        # Accepted now so later tasks never change this signature again:
        # `journal` is wired for real by Task 21 (replacing the log-and-drop
        # sink below with `journal.append(...)`); `ttl_seconds` is read by
        # Task 13's acquire_lease/heartbeat once db/lease.py exists.
        self._journal = journal
        self._ttl_seconds = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self._db is not None

    # ------------------------------------------------------------------
    # Durable writes — never raise
    # ------------------------------------------------------------------

    def upsert_issue(self, record: IssueRecord, stage: str | None) -> None:
        payload = dict(
            issue_id=record.issue_id, repo=record.repo, number=record.number,
            title=record.title, stage=stage, kind=record.action, mode=record.mode,
            branch=record.branch, pr_url=record.pr_url,
            triage_attempts=record.triage_attempts, rework_count=record.rework_count,
            continuation_count=record.continuation_count, last_error=record.error,
        )
        self._durable("issue_state.upsert", payload, lambda: issue_state.upsert(self._db, **payload))

    def _durable(self, op: str, payload: dict, call) -> None:
        """Run a durable write. Never raises — logs and drops on failure.

        Safe because GitHub labels stay truth and startup reconciliation
        rebuilds `issues.json` from scratch every restart. Task 21 replaces
        the two `self._logger.warn(...)` branches below with
        `self._journal.append(op, payload)`, once `db/journal.py` exists —
        `payload` is already the exact dict a journal entry needs.
        """
        if self._db is None:
            self._logger.warn(f"No database configured — dropping {op}")
            return
        try:
            call()
        except DbUnavailable as exc:
            self._logger.warn(f"Postgres unreachable — dropping {op} (not journaled yet): {exc}")
        except Exception as exc:
            self._logger.error(f"Durable write {op} failed (not journaled): {exc}")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_dbsync.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 537 passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add dbsync.py tests/test_dbsync.py
git commit -m "feat(db): add dbsync.py — the DbSync write seam, log-and-drop until the journal lands (ai-cc)"
```

---

### Task 9: `state.py` — `on_change` hook on `StateStore`

**Files:**
- Modify: `state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `StateStore.__init__(state_file: Path, on_change: Callable[[IssueRecord], None] | None = None) -> None`. Consumed by Task 11 (`main._make_on_change` wires `dbsync.upsert_issue` in through here) and by every existing caller of `StateStore(...)`, unaffected since the parameter is optional.

> No test file for `state.py` exists yet (`tests/test_poller_stages.py` and
> `tests/test_process_manager_ratelimit.py` import `StateStore` but exercise
> `poller.py`/`process_manager.py`, not `StateStore` itself). This creates the
> first one.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state.py
"""Tests for StateStore.on_change — the hook Task 11's DbSync wiring hangs
off of.

Wiring it correctly matters because ~20 existing call sites do
`state.add/update/transition(...)` with no idea a database now exists behind
them (docs/plans/12-shared-state-in-postgres.md design decision #6: "Making
it automatic is the point"). The hook must fire on every mutating call, and
an exploding hook must never surface to the caller — a state mutation cannot
be allowed to fail merely because Postgres is down.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state import IssueRecord, IssueStatus, StateStore  # noqa: E402


def _record(issue_id="r#1", status=IssueStatus.DISCOVERED):
    return IssueRecord(
        issue_id=issue_id, repo="r", number=1, title="t", body="",
        labels=[], action="fix", status=status,
        discovered_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        issue_updated_at="2026-01-01T00:00:00+00:00",
    )


class TestOnChangeFiresOnEveryMutation:
    def test_add_fires_on_change(self, tmp_path):
        seen = []
        store = StateStore(tmp_path / "issues.json", on_change=seen.append)
        store.add(_record())
        assert len(seen) == 1
        assert seen[0].issue_id == "r#1"

    def test_update_fires_on_change(self, tmp_path):
        seen = []
        store = StateStore(tmp_path / "issues.json", on_change=seen.append)
        store.add(_record())
        seen.clear()
        store.update("r#1", branch="feat/x")
        assert len(seen) == 1
        assert seen[0].branch == "feat/x"

    def test_transition_fires_on_change(self, tmp_path):
        seen = []
        store = StateStore(tmp_path / "issues.json", on_change=seen.append)
        store.add(_record())
        seen.clear()
        store.transition("r#1", IssueStatus.TRIAGING)
        assert len(seen) == 1
        assert seen[0].status == IssueStatus.TRIAGING

    def test_no_hook_configured_is_a_silent_noop(self, tmp_path):
        store = StateStore(tmp_path / "issues.json")
        store.add(_record())
        store.update("r#1", branch="x")
        store.transition("r#1", IssueStatus.TRIAGING)  # none of this may raise


class TestOnChangeNeverRaisesIntoTheCaller:
    def test_an_exploding_hook_does_not_break_add(self, tmp_path):
        store = StateStore(tmp_path / "issues.json",
                            on_change=lambda _r: (_ for _ in ()).throw(RuntimeError("down")))
        store.add(_record())  # must not raise
        assert store.get("r#1") is not None

    def test_an_exploding_hook_does_not_break_update(self, tmp_path):
        store = StateStore(tmp_path / "issues.json",
                            on_change=lambda _r: (_ for _ in ()).throw(RuntimeError("down")))
        store.add(_record())
        store.update("r#1", branch="feat/x")  # must not raise
        assert store.get("r#1").branch == "feat/x"

    def test_an_exploding_hook_does_not_break_transition(self, tmp_path):
        store = StateStore(tmp_path / "issues.json",
                            on_change=lambda _r: (_ for _ in ()).throw(RuntimeError("down")))
        store.add(_record())
        store.transition("r#1", IssueStatus.TRIAGING)  # must not raise
        assert store.get("r#1").status == IssueStatus.TRIAGING


class TestSaveIsUntouched:
    def test_save_does_not_invoke_on_change(self, tmp_path):
        seen = []
        store = StateStore(tmp_path / "issues.json", on_change=seen.append)
        store.add(_record())
        seen.clear()
        store.save()
        assert seen == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_state.py -v`
Expected: FAIL with `TypeError: StateStore.__init__() got an unexpected keyword argument 'on_change'`

- [ ] **Step 3: Write the implementation**

```python
# state.py — add to the imports at the top
from collections.abc import Callable
```

```python
# state.py — StateStore.__init__, replacing the current body
class StateStore:
    """JSON-backed flat-file store for IssueRecord objects."""

    def __init__(self, state_file: Path,
                 on_change: Callable[[IssueRecord], None] | None = None) -> None:
        self._state_file = Path(state_file)
        self._records: dict[str, IssueRecord] = {}  # issue_id -> IssueRecord
        self._on_change = on_change
        self._load()

    def _notify(self, record: IssueRecord) -> None:
        """Fire the on_change hook. Never lets it raise into the caller — a
        state mutation failing because a database is unreachable is exactly
        the coupling issues.json-as-cache is meant to avoid (docs/plans/
        12-shared-state-in-postgres.md, design decision #6). DbSync itself
        already swallows internally; this is the second line of defense."""
        if self._on_change is None:
            return
        try:
            self._on_change(record)
        except Exception:
            pass
```

```python
# state.py — add `self._notify(record)` as the last line of add(), update()
# and transition(). save() is untouched.

    def add(self, record: IssueRecord) -> None:
        """Add a new IssueRecord. Raises ValueError if issue_id already exists."""
        if record.issue_id in self._records:
            raise ValueError(
                f"Issue '{record.issue_id}' is already tracked. "
                "Use update() to modify existing records."
            )
        self._records[record.issue_id] = record
        self._notify(record)

    def update(self, issue_id: str, **kwargs) -> None:
        """Update arbitrary fields on an existing record and refresh updated_at."""
        record = self._records.get(issue_id)
        if record is None:
            raise KeyError(f"Issue '{issue_id}' not found in state store.")

        for key, value in kwargs.items():
            if not hasattr(record, key):
                raise AttributeError(
                    f"IssueRecord has no field '{key}'."
                )
            setattr(record, key, value)

        record.updated_at = _now_iso()
        self._notify(record)

    def transition(self, issue_id: str, new_status: str) -> None:
        """
        Change the status of an issue, enforcing VALID_TRANSITIONS.

        Raises InvalidTransitionError if the transition is not permitted.
        Raises KeyError if the issue_id is not found.
        """
        record = self._records.get(issue_id)
        if record is None:
            raise KeyError(f"Issue '{issue_id}' not found in state store.")

        current_status = record.status
        allowed = VALID_TRANSITIONS.get(current_status, [])

        if new_status not in allowed:
            raise InvalidTransitionError(
                from_status=current_status,
                to_status=new_status,
                message=(
                    f"Cannot transition issue '{issue_id}' from '{current_status}' "
                    f"to '{new_status}'. Allowed next states: {allowed or ['(none — terminal)']}"
                ),
            )

        record.status = new_status
        record.updated_at = _now_iso()
        self._notify(record)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_state.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 545 passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add state.py tests/test_state.py
git commit -m "feat(state): add StateStore.on_change, wrapped so it never raises (ai-cc)"
```

---

### Task 10: `reconcile.py` — `STAGE_TO_STATUS`, `derive_status`, `reconcile`

**Files:**
- Create: `reconcile.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `state.IssueRecord`, `state.IssueStatus`, `state.StateStore` (existing/Task 9); `db_rows: dict[str, dict]` shaped exactly as `db.issue_state.fetch_all` returns (Task 7); `gh_issues: dict[str, dict]` — the shape Task 11's `main._collect_gh_issues_for_reconcile` builds: `{"stage": str | None, "repo": str, "number": int, "title": str, "body": str, "labels": list[str], "action": str, "issue_updated_at": str, "discovered_at": str}` per issue_id. This shape is not in `CONTRACT.md` (only `db_rows`' shape is frozen there, via `db/issue_state.py`) — it is this task's own invention, documented here and consumed as-is by Task 11.
- Produces: `reconcile.STAGE_TO_STATUS: dict[str, str]`, `derive_status(stage, *, lease_held_by_other) -> str`, `reconcile(*, state, db_rows, gh_issues, harness_id, logger) -> ReconcileReport`, `ReconcileReport(rebuilt, resurrected, leases_released)`. Consumed by Task 11 (`main._reconcile_at_startup`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reconcile.py
"""Tests for reconcile.py — rebuilding issues.json from GitHub + Postgres at
startup.

Covers the exact bug from docs/plans/12-shared-state-in-postgres.md and
tests/test_shutdown_recovery.py: field_admin#215 was left at IN_PROGRESS by a
crash on 2026-07-29, with `ac-in-progress` still on the issue and no live
lease. Before reconciliation, nothing on a fresh startup would ever move it
off IN_PROGRESS again — the poller only resurrects from
FAILED/COMPLETED/INTERRUPTED. After reconciliation, GitHub's stage plus a
free lease derives QUEUED directly, on every startup, not as a one-time fix.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconcile import derive_status, reconcile  # noqa: E402
from state import IssueRecord, IssueStatus, StateStore  # noqa: E402

_NOW = datetime.now(timezone.utc)
_FUTURE = _NOW + timedelta(minutes=30)
_PAST = _NOW - timedelta(minutes=5)


def _logger():
    return SimpleNamespace(info=lambda *_a: None, warn=lambda *_a: None,
                            error=lambda *_a: None)


def _gh(stage, repo="field_admin", number=215, **extra):
    return {
        "stage": stage, "repo": repo, "number": number,
        "title": "t", "body": "", "labels": [stage] if stage else [],
        "action": "fix", "issue_updated_at": "2026-07-29T00:00:00+00:00",
        "discovered_at": "2026-07-29T00:00:00+00:00",
        **extra,
    }


def _lease_row(owner, expires_at, **extra):
    return {
        "owner_harness_id": owner, "lease_expires_at": expires_at,
        "heartbeat_at": _NOW, "branch": None, "pr_url": None,
        "triage_attempts": 0, "rework_count": 0, "continuation_count": 0,
        "last_error": None, **extra,
    }


class TestDeriveStatus:
    def test_maps_every_frozen_stage(self):
        assert derive_status("ac-pending-review", lease_held_by_other=False) == IssueStatus.DISCOVERED
        assert derive_status("ac-input-needed", lease_held_by_other=False) == IssueStatus.NEEDS_INFO
        assert derive_status("ac-dev-ready", lease_held_by_other=False) == IssueStatus.QUEUED
        assert derive_status("ac-dev-review", lease_held_by_other=False) == IssueStatus.QUEUED
        assert derive_status("ac-hitl", lease_held_by_other=False) == IssueStatus.COMPLETED
        assert derive_status("ac-merged", lease_held_by_other=False) == IssueStatus.COMPLETED
        assert derive_status("ac-done", lease_held_by_other=False) == IssueStatus.COMPLETED
        assert derive_status("ac-blocked", lease_held_by_other=False) == IssueStatus.SKIPPED
        assert derive_status(None, lease_held_by_other=False) == IssueStatus.SKIPPED

    def test_in_progress_with_a_free_lease_resurrects_to_queued(self):
        assert derive_status("ac-in-progress", lease_held_by_other=False) == IssueStatus.QUEUED

    def test_in_progress_with_a_lease_held_by_another_harness_stays_in_progress(self):
        assert derive_status("ac-in-progress", lease_held_by_other=True) == IssueStatus.IN_PROGRESS

    def test_review_in_progress_mirrors_in_progress(self):
        assert derive_status("ac-review-in-progress", lease_held_by_other=False) == IssueStatus.QUEUED
        assert derive_status("ac-review-in-progress", lease_held_by_other=True) == IssueStatus.IN_PROGRESS


class TestReconcileFirstRunEmptyDatabase:
    def test_populates_state_purely_from_github_when_postgres_is_empty(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        report = reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#31": _gh("ac-done", number=31)},
            harness_id="me", logger=_logger(),
        )
        assert report.rebuilt == 1
        assert state.get("field_admin#31").status == IssueStatus.COMPLETED


class TestStrandedInProgressIsResurrected:
    def test_ac_in_progress_with_no_postgres_row_comes_back_queued(self, tmp_path):
        # The exact field_admin#215 shape: ac-in-progress on GitHub, no row in
        # Postgres at all — the first migration ran after the crash.
        state = StateStore(tmp_path / "issues.json")
        report = reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#215": _gh("ac-in-progress")},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#215").status == IssueStatus.QUEUED
        assert "field_admin#215" in report.resurrected

    def test_ac_in_progress_with_an_expired_lease_is_also_resurrected(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        db_rows = {"field_admin#215": _lease_row("old-harness", _PAST, branch="fix/215")}
        report = reconcile(
            state=state, db_rows=db_rows,
            gh_issues={"field_admin#215": _gh("ac-in-progress")},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#215").status == IssueStatus.QUEUED
        assert state.get("field_admin#215").branch == "fix/215"
        assert "field_admin#215" in report.resurrected
        assert "field_admin#215" in report.leases_released


class TestLeaseHeldByAnotherHarnessIsNotResurrected:
    def test_an_unexpired_lease_owned_by_someone_else_stays_in_progress(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        db_rows = {"field_admin#9": _lease_row("other-harness", _FUTURE, branch="fix/9")}
        report = reconcile(
            state=state, db_rows=db_rows,
            gh_issues={"field_admin#9": _gh("ac-in-progress", number=9)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#9").status == IssueStatus.IN_PROGRESS
        assert "field_admin#9" not in report.resurrected
        assert "field_admin#9" not in report.leases_released

    def test_a_lease_held_by_this_harness_itself_resurrects_normally(self, tmp_path):
        # We own this lease — a crashed harness picking itself back up, not a
        # takeover — so it resurrects the same as a genuinely free lease.
        state = StateStore(tmp_path / "issues.json")
        db_rows = {"field_admin#9": _lease_row("me", _FUTURE, branch="fix/9")}
        reconcile(
            state=state, db_rows=db_rows,
            gh_issues={"field_admin#9": _gh("ac-in-progress", number=9)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#9").status == IssueStatus.QUEUED


class TestGithubLabelBeatsStaleLocalStatus:
    def test_local_completed_is_overwritten_when_github_now_shows_dev_ready(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        state.add(IssueRecord(
            issue_id="field_admin#5", repo="field_admin", number=5, title="t",
            body="", labels=[], action="fix", status=IssueStatus.COMPLETED,
            discovered_at="x", updated_at="x", issue_updated_at="x",
        ))
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#5": _gh("ac-dev-ready", number=5)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#5").status == IssueStatus.QUEUED


class TestTerminalLabelMapsToCompleted:
    def test_ac_hitl_maps_to_completed(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#1": _gh("ac-hitl", number=1)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#1").status == IssueStatus.COMPLETED
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_reconcile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reconcile'`

- [ ] **Step 3: Write the implementation**

```python
# reconcile.py
"""reconcile.py — rebuilds issues.json from GitHub (authoritative for stage)
and Postgres auto_claude.issue_state (authoritative for counters and lease),
on every startup.

This makes the 2026-07-29 field_admin#215 failure mode impossible by
construction: a record stranded at IN_PROGRESS with no live worker used to be
invisible to the poller forever (see tests/test_shutdown_recovery.py's
docstring). Reconciliation runs every startup, deriving status from GitHub's
`ac-*` label plus Postgres's lease columns — never from the local status left
over from the previous run — so a stranded record cannot survive a restart.

Records are *constructed* with the derived status, not transition()'d into
it. `StateStore.update()` sets fields with plain setattr and never consults
VALID_TRANSITIONS (only `transition()` does) — which is what makes it safe to
use here for a jump like IN_PROGRESS -> QUEUED that VALID_TRANSITIONS does
not otherwise allow (see state.py's transition table).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from state import IssueRecord, IssueStatus, StateStore

STAGE_TO_STATUS: dict[str, str] = {
    "ac-pending-review": IssueStatus.DISCOVERED,
    "ac-input-needed": IssueStatus.NEEDS_INFO,
    "ac-dev-ready": IssueStatus.QUEUED,
    "ac-in-progress": IssueStatus.QUEUED,          # overridden below when leased by another
    "ac-dev-review": IssueStatus.QUEUED,
    "ac-review-in-progress": IssueStatus.QUEUED,   # overridden below when leased by another
    "ac-hitl": IssueStatus.COMPLETED,
    "ac-merged": IssueStatus.COMPLETED,
    "ac-done": IssueStatus.COMPLETED,
    "ac-blocked": IssueStatus.SKIPPED,
}

# The only two stages where auto-claude self-locks by writing the label —
# every other stage's status is identical whether or not a lease exists.
_LOCKED_STAGES = frozenset({"ac-in-progress", "ac-review-in-progress"})

# Both queue the review worker, per poller.py's own routing.
_REVIEW_STAGES = frozenset({"ac-dev-review", "ac-review-in-progress"})


def derive_status(stage: str | None, *, lease_held_by_other: bool) -> str:
    """The IssueStatus a stage + lease state maps to — the table in
    docs/plans/12-shared-state-in-postgres.md / CONTRACT.md."""
    if stage in _LOCKED_STAGES and lease_held_by_other:
        return IssueStatus.IN_PROGRESS
    return STAGE_TO_STATUS.get(stage, IssueStatus.SKIPPED)


@dataclass(frozen=True)
class ReconcileReport:
    rebuilt: int
    resurrected: list[str]
    leases_released: list[str]


def _lease_held_by_other(row: dict | None, harness_id: str, now: datetime) -> bool:
    if row is None:
        return False
    owner = row.get("owner_harness_id")
    expires = row.get("lease_expires_at")
    if not owner or owner == harness_id:
        return False
    return expires is not None and expires > now


def _lease_expired(row: dict | None, now: datetime) -> bool:
    if row is None:
        return False
    owner = row.get("owner_harness_id")
    if not owner:
        return False
    expires = row.get("lease_expires_at")
    return expires is None or expires <= now


def reconcile(*, state: StateStore, db_rows: dict[str, dict],
              gh_issues: dict[str, dict], harness_id: str,
              logger) -> ReconcileReport:
    """Rebuild `state` from `gh_issues` (stage, authoritative) and `db_rows`
    (counters + lease, authoritative) — both keyed by issue_id
    ("{repo}#{number}"). `gh_issues` rows carry: stage, repo, number, title,
    body, labels, action, issue_updated_at, discovered_at (see Task 11's
    `main._collect_gh_issues_for_reconcile`). `db_rows` rows are exactly what
    `db.issue_state.fetch_all` returns."""
    now = datetime.now(timezone.utc)
    rebuilt = 0
    resurrected: list[str] = []
    leases_released: list[str] = []

    for issue_id, gh in gh_issues.items():
        db_row = db_rows.get(issue_id)
        stage = gh.get("stage")
        held_by_other = _lease_held_by_other(db_row, harness_id, now)
        status = derive_status(stage, lease_held_by_other=held_by_other)
        mode = "review" if stage in _REVIEW_STAGES else "dev"

        if stage == "ac-in-progress" and not held_by_other:
            resurrected.append(issue_id)
        if _lease_expired(db_row, now):
            leases_released.append(issue_id)

        counters = db_row or {}
        fields = dict(
            status=status,
            mode=mode,
            labels=gh.get("labels", []),
            action=gh.get("action", "implement"),
            issue_updated_at=gh.get("issue_updated_at", ""),
            branch=counters.get("branch"),
            pr_url=counters.get("pr_url"),
            triage_attempts=counters.get("triage_attempts", 0),
            rework_count=counters.get("rework_count", 0),
            continuation_count=counters.get("continuation_count", 0),
            error=counters.get("last_error"),
        )

        if state.is_known(issue_id):
            state.update(issue_id, **fields)
        else:
            state.add(IssueRecord(
                issue_id=issue_id,
                repo=gh.get("repo", issue_id.split("#", 1)[0]),
                number=gh.get("number", 0),
                title=gh.get("title", ""),
                body=gh.get("body", ""),
                discovered_at=gh.get("discovered_at", now.isoformat()),
                updated_at=now.isoformat(),
                **fields,
            ))
        rebuilt += 1

    state.save()

    if resurrected:
        logger.warn(
            f"Reconciliation resurrected {len(resurrected)} stranded issue(s) "
            f"back to queued: {', '.join(resurrected)}"
        )
    if leases_released:
        logger.info(
            f"Reconciliation found {len(leases_released)} expired lease(s): "
            f"{', '.join(leases_released)}"
        )

    return ReconcileReport(
        rebuilt=rebuilt, resurrected=resurrected, leases_released=leases_released,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_reconcile.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 556 passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add reconcile.py tests/test_reconcile.py
git commit -m "feat(db): add reconcile.py — rebuild issues.json from GitHub + Postgres (ai-cc)"
```

---

### Task 11: wire reconciliation into `main()` startup

**Files:**
- Modify: `main.py` (imports; new helpers `_init_db_layer`, `_register_harness`, `_reconcile_at_startup`, `_collect_gh_issues_for_reconcile`, `_make_on_change`, placed after `_check_schema_gate`; the "Initialize core components" block around line 482-498)
- Test: `tests/test_startup_db_wiring.py`

**Interfaces:**
- Consumes: `db.pool.Database` (Task 3), `db.schema.check_schema_current`/`SchemaOutOfDate` + `main._check_schema_gate` (Task 5), `db.harness.new_harness`/`Harness` (Task 6), `db.issue_state.fetch_all` (Task 7), `dbsync.DbSync` (Task 8 — imported directly at module scope; it already exists by this point, so there is no forward reference to guard against), `state.StateStore(..., on_change=...)` (Task 9), `reconcile.reconcile` (Task 10).
- Produces: `main._init_db_layer(config, logger) -> (db, harness, dbsync)`, `main._collect_gh_issues_for_reconcile(config, github, logger) -> dict[str, dict]` (the `gh_issues` shape Task 10 documents), `main._reconcile_at_startup(config, github, state, db, harness, dbsync, logger) -> ReconcileReport`, `main._make_on_change(dbsync) -> Callable[[IssueRecord], None]`.

> **Note on `journal`.** `db/journal.py` does not exist until Task 20, so
> `dbsync` is constructed here with no `journal` argument at all — it
> defaults to `None` (Task 8), and a durable write that cannot reach
> Postgres is logged and dropped in the meantime, which is safe because
> `_reconcile_at_startup` below rebuilds `issues.json` from GitHub +
> Postgres on every single restart regardless. Task 21 is what constructs a
> real `Journal` in `main` and threads it through `_init_db_layer` into
> `DbSync` — nothing here needs to anticipate that.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_startup_db_wiring.py
"""Tests for main.py's database-layer startup wiring (Task 11): constructing
Database/Harness/DbSync, registering the harness, reconciling issues.json
from GitHub + Postgres (releasing expired leases first), and wiring
on_change into StateStore.

Degraded mode (`[database].enabled = false`, or the URL environment variable
unset) must never stop the daemon from starting: GitHub labels and the PR are
still truth, and Postgres is explicitly an add-on (docs/plans/
12-shared-state-in-postgres.md, "Three stores").
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from db.schema import SchemaOutOfDate  # noqa: E402
from github_client import GithubClientError  # noqa: E402


def _logger():
    msgs = []
    return SimpleNamespace(
        info=lambda m: msgs.append(("info", m)),
        warn=lambda m: msgs.append(("warn", m)),
        error=lambda m: msgs.append(("error", m)),
        close=lambda: None,
        messages=msgs,
    )


def _db_config(enabled=True, url="postgresql://x"):
    return SimpleNamespace(
        enabled=enabled,
        url=lambda: url,
        connect_timeout_seconds=10,
        journal_file=Path("state/journal.jsonl"),
        lease_ttl_seconds=1800,
    )


def _config(db_config):
    return SimpleNamespace(database=db_config)


class TestInitDbLayerDegradedMode:
    def test_disabled_in_config_yields_no_database(self, monkeypatch):
        cfg = _config(_db_config(enabled=False))
        monkeypatch.setattr(main, "Database", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not construct a Database when disabled")))
        db, harness, dbsync = main._init_db_layer(cfg, _logger())
        assert db is None
        assert dbsync.enabled is False
        assert harness is not None  # a harness identity always exists, DB or not

    def test_url_unset_yields_no_database_even_when_enabled(self, monkeypatch):
        cfg = _config(_db_config(enabled=True, url=None))
        monkeypatch.setattr(main, "Database", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not construct a Database with no URL")))
        db, _harness, dbsync = main._init_db_layer(cfg, _logger())
        assert db is None
        assert dbsync.enabled is False

    def test_init_db_layer_wires_the_configured_ttl_into_dbsync(self, monkeypatch):
        # Fix for config.database.lease_ttl_seconds otherwise being dead —
        # Task 13's acquire_lease/heartbeat read dbsync._ttl_seconds, so it
        # has to land here even though nothing consumes it yet.
        cfg = _config(_db_config(enabled=False))
        cfg.database.lease_ttl_seconds = 900
        _db, _harness, dbsync = main._init_db_layer(cfg, _logger())
        assert dbsync._ttl_seconds == 900

    def test_reconcile_at_startup_tolerates_no_database(self, tmp_path):
        state = main.StateStore(tmp_path / "issues.json")
        github = SimpleNamespace(list_issues=lambda repo, assignee=None: [])
        config = SimpleNamespace(
            github=SimpleNamespace(repos=["field_admin"], bot_login="bot"),
        )
        harness = SimpleNamespace(id="me")
        dbsync = main.DbSync(None, harness, _logger())
        # db=None must not raise — reconciliation runs against an empty
        # db_rows dict, identical to Postgres genuinely being empty.
        main._reconcile_at_startup(config, github, state, None, harness, dbsync, _logger())


class TestInitDbLayerStaleSchemaAborts:
    def test_schema_out_of_date_aborts_before_reaching_dbsync(self, monkeypatch):
        cfg = _config(_db_config(enabled=True, url="postgresql://x"))
        monkeypatch.setattr(main, "Database", lambda *a, **k: object())
        monkeypatch.setattr(
            main, "check_schema_current",
            lambda db: (_ for _ in ()).throw(SchemaOutOfDate("run: alembic upgrade head")),
        )
        with pytest.raises(SystemExit) as excinfo:
            main._init_db_layer(cfg, _logger())
        assert excinfo.value.code == 1


class TestReconcileAtStartupReleasesExpiredLeasesFirst:
    """Guards Fix 5: reconcile()'s own leases_released is derived from
    db_rows, which by the time it is read here already reflects any release
    dbsync.release_expired() just performed — so reconcile() alone cannot
    tell "just freed" apart from "never held". The ids release_expired()
    actually returns are substituted into the report afterwards, which is
    what keeps ReconcileReport.leases_released describing a real Postgres
    UPDATE instead of merely echoing db_rows back."""

    def test_release_expired_runs_before_the_fetch_and_its_ids_land_in_the_report(
        self, tmp_path, monkeypatch,
    ):
        order = []
        state = main.StateStore(tmp_path / "issues.json")
        github = SimpleNamespace(list_issues=lambda repo, assignee=None: [])
        config = SimpleNamespace(github=SimpleNamespace(repos=[], bot_login="bot"))
        harness = SimpleNamespace(id="me")

        def fake_fetch_all(db):
            order.append("fetch")
            return {}

        monkeypatch.setattr(main.db_issue_state, "fetch_all", fake_fetch_all)

        def fake_release_expired():
            order.append("release")
            return ["field_admin#7"]

        dbsync = SimpleNamespace(release_expired=fake_release_expired)
        report = main._reconcile_at_startup(
            config, github, state, object(), harness, dbsync, _logger(),
        )

        assert order == ["release", "fetch"], "leases must be released before db_rows is fetched"
        assert report.leases_released == ["field_admin#7"]

    def test_no_release_expired_method_yet_is_a_no_op(self, tmp_path):
        # db/lease.py (Task 12) and Task 13's DbSync.release_expired do not
        # exist until later — a plain Task 8 DbSync has no such attribute,
        # and this must not raise or fabricate a release.
        state = main.StateStore(tmp_path / "issues.json")
        github = SimpleNamespace(list_issues=lambda repo, assignee=None: [])
        config = SimpleNamespace(github=SimpleNamespace(repos=[], bot_login="bot"))
        harness = SimpleNamespace(id="me")
        dbsync = main.DbSync(None, harness, _logger())

        report = main._reconcile_at_startup(
            config, github, state, None, harness, dbsync, _logger(),
        )

        assert report.leases_released == []


class TestCollectGhIssuesForReconcile:
    def test_shapes_one_row_per_issue_keyed_by_issue_id(self):
        github = SimpleNamespace(list_issues=lambda repo, assignee=None: [
            {"number": 215, "title": "t", "body": "b", "updated_at": "u",
             "created_at": "c", "labels": [{"name": "ac-in-progress"}]},
        ])
        config = SimpleNamespace(
            github=SimpleNamespace(repos=["field_admin"], bot_login="bot"),
        )
        result = main._collect_gh_issues_for_reconcile(config, github, _logger())
        assert set(result) == {"field_admin#215"}
        assert result["field_admin#215"]["stage"] == "ac-in-progress"

    def test_a_failing_repo_does_not_stop_the_others(self):
        def list_issues(repo, assignee=None):
            if repo == "boom":
                raise GithubClientError("down")
            return []

        github = SimpleNamespace(list_issues=list_issues)
        config = SimpleNamespace(
            github=SimpleNamespace(repos=["boom", "field_admin"], bot_login="bot"),
        )
        result = main._collect_gh_issues_for_reconcile(config, github, _logger())
        assert result == {}


class TestOnChangeWrapperUpsertsWithDerivedStage:
    def test_calls_dbsync_upsert_issue_with_the_stage_derived_from_labels(self):
        calls = []
        dbsync = SimpleNamespace(upsert_issue=lambda record, stage: calls.append((record, stage)))
        handler = main._make_on_change(dbsync)
        record = SimpleNamespace(labels=["ac-in-progress", "ac-fix"])
        handler(record)
        assert calls == [(record, "ac-in-progress")]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_startup_db_wiring.py -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute '_init_db_layer'`

- [ ] **Step 3: Write the implementation**

```python
# main.py — extend the import block near the top
import stages  # already imported
import version
from db import harness as db_harness
from db import issue_state as db_issue_state
from db.harness import new_harness
from db.pool import Database  # DbUnavailable already imported (Task 5)
from dbsync import DbSync  # Task 8 — dbsync.py already exists, no forward reference
from reconcile import reconcile
```

```python
# main.py — new helpers, placed after `_check_schema_gate` (Task 5)
def _register_harness(db, harness, logger: MainLogger) -> None:
    """Best-effort harness registration. A registration that cannot reach
    Postgres is logged and dropped, not journaled — db/journal.py does not
    exist until Task 20, and the harness attempts registration again on the
    next restart regardless. Task 21 revisits this once a real Journal
    exists."""
    if db is None:
        return
    try:
        db_harness.register(db, harness)
    except DbUnavailable as exc:
        logger.warn(f"Postgres unreachable while registering the harness: {exc}")


def _init_db_layer(config, logger: MainLogger):
    """Construct the Postgres-backed layer main() writes through, or a fully
    degraded stand-in when it is disabled/unreachable. Returns
    (db, harness, dbsync) — db is None in degraded mode; harness and dbsync
    always exist so callers never branch on "did this construct".

    `dbsync.py` (Task 8) already exists at this point in the sequence, so
    `DbSync` is imported normally at module scope, not lazily. It is
    constructed with no `journal` (defaults to `None` — Task 21 is what
    threads a real one through here) and with `ttl_seconds` taken from
    config, which is what makes `config.database.lease_ttl_seconds` reach
    `db.lease.acquire` once Task 13 wires the lease methods onto `DbSync`.
    """
    harness = new_harness(version.__version__)

    db = None
    url = config.database.url() if config.database.enabled else None
    if url:
        db = Database(url, connect_timeout=config.database.connect_timeout_seconds)
        _check_schema_gate(db, logger)
        _register_harness(db, harness, logger)
    else:
        logger.info(
            "Database sync disabled or PIPELINE_METRICS_DATABASE_URL unset — "
            "running on local state only"
        )

    dbsync = DbSync(db, harness, logger, ttl_seconds=config.database.lease_ttl_seconds)
    return db, harness, dbsync


def _collect_gh_issues_for_reconcile(config, github, logger: MainLogger) -> dict:
    """The `gh_issues` shape reconcile.reconcile() expects — see reconcile.py.
    A repo that cannot be read is skipped, not fatal; a poll a few seconds
    later behaves the same way already."""
    gh_issues: dict[str, dict] = {}
    for repo in config.github.repos:
        try:
            issues = github.list_issues(repo, assignee=config.github.bot_login)
        except GithubClientError as exc:
            logger.warn(f"Could not read {repo} for reconciliation: {exc}")
            continue
        for issue in issues:
            label_names = [lbl["name"] for lbl in issue.get("labels", [])]
            issue_id = f"{repo}#{issue['number']}"
            gh_issues[issue_id] = {
                "stage": stages.stage_of(label_names),
                "repo": repo,
                "number": issue["number"],
                "title": issue["title"],
                "body": issue.get("body", "") or "",
                "labels": label_names,
                "action": stages.kind_of(label_names),
                "issue_updated_at": issue.get("updated_at", ""),
                "discovered_at": issue.get("created_at", ""),
            }
    return gh_issues


def _reconcile_at_startup(config, github, state, db, harness, dbsync, logger: MainLogger):
    """Rebuild `state` from GitHub + Postgres before the poll loop starts.
    See reconcile.py and docs/plans/12-shared-state-in-postgres.md, "Startup
    reconciliation". db=None (disabled or unreachable) reconciles against an
    empty db_rows dict — identical to a genuinely empty database on the very
    first run, which the design already treats as the normal case.

    Expired leases are released in Postgres FIRST, before `db_rows` is
    fetched: `dbsync.release_expired()` issues the actual `UPDATE` that
    clears them (db/lease.py, Task 12), so by the time `db_rows` is read
    below those rows already show a free lease, and `reconcile()`'s own
    db_rows-derived `leases_released` can no longer tell "just freed" apart
    from "never held". The ids `release_expired()` actually returned are
    substituted into the report afterwards, which is what keeps
    `ReconcileReport.leases_released` true rather than merely informational.
    `release_expired` does not exist on `dbsync` until Task 13 wires
    db/lease.py into it — `getattr(..., None)` makes the call a no-op until
    then, and DbSync.release_expired is itself a no-op whenever Postgres is
    disabled or unreachable (it fails closed and returns `[]`), so this
    call is safe in every configuration.
    """
    release_expired = getattr(dbsync, "release_expired", None)
    released = release_expired() if release_expired is not None else []

    gh_issues = _collect_gh_issues_for_reconcile(config, github, logger)

    db_rows: dict = {}
    if db is not None:
        try:
            db_rows = db_issue_state.fetch_all(db)
        except DbUnavailable as exc:
            logger.warn(f"Could not fetch issue_state for reconciliation: {exc}")

    report = reconcile(state=state, db_rows=db_rows, gh_issues=gh_issues,
                        harness_id=harness.id, logger=logger)
    if released:
        report = dataclasses.replace(report, leases_released=released)
    return report


def _make_on_change(dbsync):
    """Bridges StateStore's single-record hook to DbSync.upsert_issue, which
    additionally wants the GitHub stage label — not part of IssueRecord
    itself, but derivable from record.labels via stages.stage_of."""
    def _handler(record) -> None:
        dbsync.upsert_issue(record, stages.stage_of(record.labels))
    return _handler
```

Add `import dataclasses` to `main.py`'s standard-library imports if not already
present — `_reconcile_at_startup` above is the first thing in `main.py` to
need `dataclasses.replace`.

```python
# main.py — replace the "Initialize core components" block (currently lines
# 482-498) with:
    # Initialize core components
    github = GithubClient(config.github.org)

    db, harness, dbsync = _init_db_layer(config, logger)

    state = StateStore(config.paths.state_file, on_change=_make_on_change(dbsync))
    _reconcile_at_startup(config, github, state, db, harness, dbsync, logger)

    poller = Poller(config, github, state, logger)
    triage_engine = TriageEngine(config, github)

    # Create multiprocessing queues
    log_queue = multiprocessing.Queue()
    state_queue = multiprocessing.Queue()

    process_manager = ProcessManager(
        config=config,
        state=state,
        logger=logger,
        log_queue=log_queue,
        state_queue=state_queue,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_startup_db_wiring.py -v`
Expected: PASS (10 tests) — unlike earlier drafts of this task, nothing here
is deferred: `dbsync.py` (Task 8) already exists, so every test in this file
passes immediately, with no forward-reference gap to wait out.

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 566 passed, 0 failed.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_startup_db_wiring.py
git commit -m "feat(db): wire startup reconciliation and on_change into main() (ai-cc)"
```
## Phase C — Lease and fencing

Ships the lease (`db/lease.py`) and the fencing that makes it real (worker-side
guard on every irreversible remote act) as one change, per the spec: "a lease
without fencing is decorative." Task 12 builds the lease primitives and proves
the atomic claim against a real Postgres. Task 13 wires acquire/heartbeat/
release into `main`/`process_manager`. Task 14 wires the fence into every
`git push`, `gh pr create`, `gh pr review`, and the `_set_labels` chokepoint.
Task 15 replaces `_release_stale_locks`'s startup-is-safe assumption with an
expiry-driven check, while preserving the no-database fallback.

---

### Task 12: `db/lease.py`

**Files:**
- Create: `db/lease.py`
- Create: `pytest.ini`
- Test: `tests/test_db_lease.py`
- Test: `tests/test_lease_concurrency.py`

**Interfaces:**
- Consumes: `db.pool.Database` (`execute(sql, params=()) -> list[tuple]`,
  autocommit, retries internally then raises `DbUnavailable`) and
  `db.pool.DbUnavailable`, both from an earlier task.
- Produces (consumed by Task 13's `dbsync.py` additions and Task 14's
  `worker._assert_lease_held`):
  ```python
  LEASE_TTL_SECONDS: int = 1800
  HEARTBEAT_INTERVAL_SECONDS: int = 60
  def acquire(db, issue_id: str, harness_id: str, ttl_seconds: int = LEASE_TTL_SECONDS) -> bool
  def heartbeat(db, harness_id: str, ttl_seconds: int = LEASE_TTL_SECONDS) -> int
  def release(db, issue_id: str, harness_id: str) -> None
  def check(db, issue_id: str, harness_id: str, *, retries: int = 3, sleep=time.sleep) -> bool
  def release_expired(db) -> list[str]
  ```

- [ ] **Step 1: Register the `postgres` marker**

No `conftest.py` or pytest config exists in this repo today, so
`@pytest.mark.postgres` (used by the concurrency test below) would collect
fine but print `PytestUnknownMarkWarning` on every run, and `-m "not
postgres"` would silently select nothing. Add a bare `pytest.ini` at the repo
root with only a `markers` key — it registers the name and enables `-m`
filtering, and changes nothing else about discovery, `testpaths`, or
collection, so the existing 493 tests are unaffected by its mere presence.

```ini
[pytest]
markers =
    postgres: requires a real Postgres reachable via AUTO_CLAUDE_TEST_DATABASE_URL (deselect with -m "not postgres")
```

- [ ] **Step 2: Write the failing unit tests**

```python
"""Tests for the Postgres issue lease - db/lease.py.

Guards the 2026-07-29 finding that GitHub labels cannot be a distributed
lock: `main._release_stale_locks` used to assume "at startup no worker of
ours is alive, so any bot-assigned ac-in-progress issue is stale by
definition" - an assumption a second harness breaks immediately, and
read-labels-then-write-label has no compare-and-swap to make it safe anyway
(GitHub offers none). `db/lease.py` replaces both with one atomic SQL
statement (see docs/plans/12-shared-state-in-postgres.md, "Lease protocol").

These tests exercise the Python side against a fake, in-memory Database. The
one thing a fake cannot honestly verify - that the UPDATE really is atomic
under two simultaneous connections - is tests/test_lease_concurrency.py.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import lease  # noqa: E402
from db.pool import DbUnavailable  # noqa: E402


class _FakeDb:
    """In-memory stand-in for Database. Recognises lease.py's SQL by
    identity (the exact private constants it exports) so it can simulate
    the atomic claim/heartbeat/release/check/expiry semantics without a real
    connection. This is a fake of the *lease protocol*, not of SQL in
    general - it would not help test anything else in db/."""

    def __init__(self):
        # issue_id -> {"owner": str | None, "expires_at": float | None}
        self.rows: dict[str, dict] = {}

    def seed(self, issue_id, owner=None, expires_in=None):
        self.rows[issue_id] = {
            "owner": owner,
            "expires_at": (time.time() + expires_in) if expires_in is not None else None,
        }

    def _row(self, issue_id):
        return self.rows.setdefault(issue_id, {"owner": None, "expires_at": None})

    def execute(self, sql, params=()):
        now = time.time()
        if sql == lease._ACQUIRE_SQL:
            harness_id, ttl_seconds, issue_id = params
            row = self._row(issue_id)
            free = row["owner"] is None or (
                row["expires_at"] is not None and row["expires_at"] < now
            )
            if not free:
                return []
            row["owner"] = harness_id
            row["expires_at"] = now + ttl_seconds
            return [(issue_id,)]

        if sql == lease._HEARTBEAT_SQL:
            ttl_seconds, harness_id = params
            updated = []
            for issue_id, row in self.rows.items():
                if (row["owner"] == harness_id
                        and row["expires_at"] is not None
                        and row["expires_at"] >= now):
                    row["expires_at"] = now + ttl_seconds
                    updated.append((issue_id,))
            return updated

        if sql == lease._RELEASE_SQL:
            issue_id, harness_id = params
            row = self.rows.get(issue_id)
            if row and row["owner"] == harness_id:
                row["owner"] = None
                row["expires_at"] = None
            return []

        if sql == lease._CHECK_SQL:
            issue_id, harness_id = params
            row = self.rows.get(issue_id)
            if (row and row["owner"] == harness_id
                    and row["expires_at"] is not None and row["expires_at"] >= now):
                return [(1,)]
            return []

        if sql == lease._RELEASE_EXPIRED_SQL:
            freed = []
            for issue_id, row in self.rows.items():
                if (row["owner"] is not None
                        and row["expires_at"] is not None and row["expires_at"] < now):
                    row["owner"] = None
                    row["expires_at"] = None
                    freed.append((issue_id,))
            return freed

        raise AssertionError(f"unrecognised SQL passed to fake db: {sql!r}")


class _AlwaysDownDb:
    def execute(self, sql, params=()):
        raise DbUnavailable("connection refused")


class TestAcquire:
    def test_wins_a_free_issue(self):
        db = _FakeDb()
        assert lease.acquire(db, "r#1", "harness-a") is True
        assert db.rows["r#1"]["owner"] == "harness-a"

    def test_loses_to_an_unexpired_holder(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=1800)
        assert lease.acquire(db, "r#1", "harness-b") is False
        assert db.rows["r#1"]["owner"] == "harness-a", "loser must not overwrite the winner"

    def test_reclaims_an_expired_lease(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=-10)
        assert lease.acquire(db, "r#1", "harness-b") is True
        assert db.rows["r#1"]["owner"] == "harness-b"

    def test_propagates_db_unavailable_rather_than_reporting_false(self):
        # A swallowed exception here would be indistinguishable from "someone
        # else holds it" - Task 13's caller needs to tell those apart: one
        # means "try again next tick", the other means "spawn elsewhere".
        with pytest.raises(DbUnavailable):
            lease.acquire(_AlwaysDownDb(), "r#1", "harness-a")


class TestHeartbeat:
    def test_extends_every_lease_this_harness_owns(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=100)
        db.seed("r#2", owner="harness-a", expires_in=100)
        db.seed("r#3", owner="harness-b", expires_in=100)

        updated = lease.heartbeat(db, "harness-a")

        assert updated == 2
        assert db.rows["r#1"]["expires_at"] > time.time() + 1000
        assert db.rows["r#3"]["expires_at"] < time.time() + 1000, \
            "must not touch another harness's lease"

    def test_does_not_resurrect_an_already_expired_lease(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=-10)
        assert lease.heartbeat(db, "harness-a") == 0


class TestRelease:
    def test_clears_our_own_lease(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=1800)
        lease.release(db, "r#1", "harness-a")
        assert db.rows["r#1"]["owner"] is None

    def test_does_not_clear_a_lease_we_no_longer_hold(self):
        # We lost the race to expiry and someone else already re-acquired -
        # an unconditional release would clear *their* lease out from under
        # them.
        db = _FakeDb()
        db.seed("r#1", owner="harness-b", expires_in=1800)
        lease.release(db, "r#1", "harness-a")
        assert db.rows["r#1"]["owner"] == "harness-b"


class TestCheck:
    def test_true_when_this_harness_holds_an_unexpired_lease(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=1800)
        assert lease.check(db, "r#1", "harness-a") is True

    def test_false_when_another_harness_holds_it(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-b", expires_in=1800)
        assert lease.check(db, "r#1", "harness-a") is False

    def test_false_when_expired(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=-10)
        assert lease.check(db, "r#1", "harness-a") is False

    def test_fails_closed_after_three_retries_when_db_unreachable(self):
        sleeps = []
        result = lease.check(
            _AlwaysDownDb(), "r#1", "harness-a", retries=3, sleep=sleeps.append,
        )
        assert result is False
        assert len(sleeps) == 3, "must retry exactly `retries` times before giving up"

    def test_recovers_if_the_db_comes_back_before_retries_are_exhausted(self):
        calls = {"n": 0}

        class _FlakyDb:
            def execute(self, sql, params=()):
                calls["n"] += 1
                if calls["n"] < 2:
                    raise DbUnavailable("still down")
                return [(1,)]

        held = lease.check(_FlakyDb(), "r#1", "harness-a", retries=3, sleep=lambda s: None)
        assert held is True


class TestReleaseExpired:
    def test_frees_only_leases_past_their_expiry(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=-10)   # expired
        db.seed("r#2", owner="harness-b", expires_in=1800)  # live
        db.seed("r#3", owner=None)                            # never leased

        freed = lease.release_expired(db)

        assert freed == ["r#1"]
        assert db.rows["r#1"]["owner"] is None
        assert db.rows["r#2"]["owner"] == "harness-b", "must not touch a live lease"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_db_lease.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.lease'`

- [ ] **Step 4: Write the implementation**

```python
"""auto_claude.issue_state lease: a per-issue distributed lock inlined into
the row it protects, rather than a separate table - so the claim is one
atomic single-row UPDATE with nothing to join (see
docs/plans/12-shared-state-in-postgres.md, "Lease protocol").

`acquire` is the spec's exact statement, parameterised only on the TTL. Zero
rows returned means someone else holds an unexpired lease - that is the
entire locking protocol. This exists because GitHub labels cannot be a lock:
read-labels-then-write-label is a check-then-act race with no compare-and-
swap, so two harnesses would eventually both claim the same issue.
"""

from __future__ import annotations

import time

from db.pool import Database, DbUnavailable

LEASE_TTL_SECONDS: int = 1800          # 30 minutes
HEARTBEAT_INTERVAL_SECONDS: int = 60

# Total wall-clock budget for `check`'s fail-closed retry, spread across
# `retries` attempts after the first. ~5s per the spec ("lease.check retries
# 3x over ~5s").
_CHECK_RETRY_BUDGET_SECONDS = 5.0

_ACQUIRE_SQL = """
UPDATE auto_claude.issue_state
   SET owner_harness_id = %s,
       lease_expires_at = now() + make_interval(secs => %s),
       heartbeat_at     = now()
 WHERE issue_id = %s
   AND (owner_harness_id IS NULL OR lease_expires_at < now())
RETURNING issue_id
"""

_HEARTBEAT_SQL = """
UPDATE auto_claude.issue_state
   SET lease_expires_at = now() + make_interval(secs => %s),
       heartbeat_at     = now()
 WHERE owner_harness_id = %s
   AND lease_expires_at >= now()
RETURNING issue_id
"""

_RELEASE_SQL = """
UPDATE auto_claude.issue_state
   SET owner_harness_id = NULL,
       lease_expires_at = NULL,
       heartbeat_at     = NULL
 WHERE issue_id = %s
   AND owner_harness_id = %s
"""

_CHECK_SQL = """
SELECT 1 FROM auto_claude.issue_state
 WHERE issue_id = %s
   AND owner_harness_id = %s
   AND lease_expires_at >= now()
"""

_RELEASE_EXPIRED_SQL = """
UPDATE auto_claude.issue_state
   SET owner_harness_id = NULL,
       lease_expires_at = NULL,
       heartbeat_at     = NULL
 WHERE owner_harness_id IS NOT NULL
   AND lease_expires_at < now()
RETURNING issue_id
"""


def acquire(db: Database, issue_id: str, harness_id: str,
            ttl_seconds: int = LEASE_TTL_SECONDS) -> bool:
    """Claim the lease on `issue_id` for `harness_id`. True iff we now hold it.

    Fails CLOSED: `DbUnavailable` propagates uncaught rather than being
    reported as False. A swallowed exception here would be indistinguishable
    from "someone else holds it", and the two demand different responses
    from a caller - retry shortly, versus move on to the next issue.
    """
    rows = db.execute(_ACQUIRE_SQL, (harness_id, ttl_seconds, issue_id))
    return len(rows) == 1


def heartbeat(db: Database, harness_id: str,
              ttl_seconds: int = LEASE_TTL_SECONDS) -> int:
    """Extend every unexpired lease `harness_id` owns. Returns rows updated.

    Excludes already-expired rows on purpose (`lease_expires_at >= now()`):
    a harness that stalled past the TTL must not resurrect a lease another
    harness may have already reclaimed by heartbeating its way back in - it
    has to `acquire` again like anyone else.
    """
    rows = db.execute(_HEARTBEAT_SQL, (ttl_seconds, harness_id))
    return len(rows)


def release(db: Database, issue_id: str, harness_id: str) -> None:
    """Release the lease, but only if `harness_id` still holds it.

    The `owner_harness_id = %s` guard matters: if our lease already expired
    and someone else re-acquired it, an unconditional release would clear
    *their* lease out from under them. A worker that finishes after losing
    that race simply no-ops here.
    """
    db.execute(_RELEASE_SQL, (issue_id, harness_id))


def check(db: Database, issue_id: str, harness_id: str,
          *, retries: int = 3, sleep=time.sleep) -> bool:
    """True iff `harness_id` still holds an unexpired lease on `issue_id`.

    Fails CLOSED, unlike every other function here: an unreachable database
    is retried `retries` times over ~5s and then reported as False rather
    than re-raised. This is deliberate and is the *only* place in this
    module that swallows `DbUnavailable` - the fencing caller (Task 14's
    `worker._assert_lease_held`) cannot distinguish "this box is
    partitioned" from "someone else legitimately took over", and refusing
    the irreversible act is the safe response to both.
    """
    delay = _CHECK_RETRY_BUDGET_SECONDS / retries if retries else 0
    for attempt in range(retries + 1):
        try:
            rows = db.execute(_CHECK_SQL, (issue_id, harness_id))
            return len(rows) == 1
        except DbUnavailable:
            if attempt >= retries:
                return False
            sleep(delay)
    return False  # pragma: no cover - unreachable, loop always returns above


def release_expired(db: Database) -> list[str]:
    """Clear every lease past its `lease_expires_at`. Returns issue_ids freed."""
    rows = db.execute(_RELEASE_EXPIRED_SQL)
    return [row[0] for row in rows]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_db_lease.py -v`
Expected: PASS (14 passed)

- [ ] **Step 6: Write the concurrency test**

```python
"""Lease concurrency - the one guarantee a fake Database cannot honestly
verify: that `lease.acquire`'s single UPDATE ... RETURNING is genuinely
atomic under two simultaneous claims.

Runs against a real Postgres. A Python-level fake would pass even a broken,
non-atomic implementation (e.g. a naive SELECT-then-UPDATE) by construction,
because Python threads serialise around the GIL between I/O waits in a way
that happens to look atomic. Real network round-trips to Postgres do not.
Skipped by default - AUTO_CLAUDE_TEST_DATABASE_URL must point at a
throwaway schema; CI or a developer wires it up deliberately. See
docs/plans/12-shared-state-in-postgres.md, "Testing": "the atomic claim is
the one thing a fake cannot honestly verify."
"""

from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import lease  # noqa: E402
from db.pool import Database  # noqa: E402

TEST_DB_URL_ENV = "AUTO_CLAUDE_TEST_DATABASE_URL"

pytestmark = pytest.mark.postgres


def _require_test_db_url() -> str:
    url = os.environ.get(TEST_DB_URL_ENV)
    if not url:
        pytest.skip(f"{TEST_DB_URL_ENV} not set - skipping real-Postgres lease test")
    return url


@pytest.fixture
def db():
    database = Database(_require_test_db_url())
    yield database
    database.close()


@pytest.fixture
def issue_id(db):
    """A throwaway issue_state row this test owns end to end."""
    new_id = f"test#{uuid.uuid4().hex}"
    db.execute(
        "INSERT INTO auto_claude.issue_state (issue_id, repo, number, title) "
        "VALUES (%s, %s, %s, %s)",
        (new_id, "test-repo", 1, "lease concurrency test"),
    )
    yield new_id
    db.execute("DELETE FROM auto_claude.issue_state WHERE issue_id = %s", (new_id,))


class TestConcurrentAcquire:
    def test_exactly_one_of_two_simultaneous_claims_wins(self, issue_id):
        """Two harnesses race to claim a fresh issue. Exactly one must win -
        the failure mode this guards is two dev workers both spawning on the
        same issue, which the label-based lock could not prevent."""
        url = _require_test_db_url()
        harness_a, harness_b = uuid.uuid4().hex, uuid.uuid4().hex

        def claim(harness_id: str) -> bool:
            # Each thread opens its own connection - sharing one Database
            # across threads would serialise the two claims through a
            # single session and prove nothing about cross-connection
            # atomicity, which is exactly what is under test.
            database = Database(url)
            try:
                return lease.acquire(database, issue_id, harness_id)
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(claim, harness_a)
            future_b = pool.submit(claim, harness_b)
            result_a = future_a.result()
            result_b = future_b.result()

        assert [result_a, result_b].count(True) == 1, (
            f"expected exactly one winner, got a={result_a} b={result_b}"
        )

    def test_loser_sees_the_winners_harness_id(self, db, issue_id):
        harness_a, harness_b = uuid.uuid4().hex, uuid.uuid4().hex
        assert lease.acquire(db, issue_id, harness_a) is True
        assert lease.acquire(db, issue_id, harness_b) is False

        rows = db.execute(
            "SELECT owner_harness_id FROM auto_claude.issue_state WHERE issue_id = %s",
            (issue_id,),
        )
        assert rows[0][0] == harness_a
```

- [ ] **Step 7: Run the full suite and confirm the new test is hermetic by default**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: `580 passed, 2 skipped` — the two `TestConcurrentAcquire` tests skip
with `AUTO_CLAUDE_TEST_DATABASE_URL not set - skipping real-Postgres lease
test` because that variable is not set in the default dev/CI environment; 0
failed.

- [ ] **Step 8: Commit**

```bash
git add pytest.ini db/lease.py tests/test_db_lease.py tests/test_lease_concurrency.py
git commit -m "feat(lease): add db/lease.py with an atomic Postgres claim (ai-cc)"
```

---

### Task 13: lease lifecycle in `main`/`process_manager`

**Files:**
- Modify: `dbsync.py` (add lease pass-through methods to the existing `DbSync` class)
- Modify: `process_manager.py:23-49` (constructor), `:124-205` (`spawn`), `:207-275` (`reap_dead`)
- Modify: `main.py:122` (new helper), `:392-596` (`main`)
- Test: `tests/test_dbsync.py` (append)
- Test: `tests/test_process_manager_ratelimit.py` (append)
- Test: `tests/test_heartbeat.py` (new)

**Interfaces:**
- Consumes: Task 12's `db.lease.{acquire,heartbeat,release,check,release_expired}`.
  `dbsync.py` (Task 8), `db/harness.py` (`Harness`, `new_harness`, `register`,
  Task 6), and the wiring that constructs `db`/`harness`/`dbsync` inside
  `main()` (Task 11 registers the harness at startup and constructs
  `DbSync(db, harness, logger, ttl_seconds=config.database.lease_ttl_seconds)`,
  wiring `StateStore(..., on_change=dbsync.upsert_issue-based callback)`) are
  already in place by this point.
- Produces (consumed by Task 14 and Task 15):
  ```python
  # dbsync.py, added to the existing DbSync class
  def acquire_lease(self, issue_id: str) -> bool
  def heartbeat(self) -> None
  def release_lease(self, issue_id: str) -> None
  def check_lease(self, issue_id: str) -> bool
  def release_expired(self) -> list[str]

  # process_manager.py
  class ProcessManager:
      def __init__(self, ..., dbsync: DbSync | None = None, harness_id: str | None = None) -> None

  # main.py
  def _maybe_heartbeat(dbsync, last_at: float, interval: float, now: float | None = None) -> float
  ```
  `IssueContext.harness_id` (from Task 14) is set on every spawned context
  from `ProcessManager._harness_id`.

- [ ] **Step 1: Write the failing test for `DbSync`'s lease methods**

```python
"""Appended to tests/test_dbsync.py: lease pass-through on DbSync.

Guards three things at once: that DbSync.acquire_lease/heartbeat/release_lease/
check_lease/release_expired actually call db.lease's functions (not
reimplement the SQL), that every one of them is a safe no-op when Postgres
is disabled (`db=None`) - a disabled database means no shared state, which
per docs/plans/12-shared-state-in-postgres.md means there cannot be a second
harness, so lease operations must not block anything - and that a
non-default configured `ttl_seconds` (Task 8's `DbSync.__init__`, otherwise
unused until now) actually reaches `db.lease.acquire`/`heartbeat`, which is
what makes `config.database.lease_ttl_seconds` (Task 2) more than a dead
field on the config object.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.harness import Harness  # noqa: E402
from dbsync import DbSync  # noqa: E402


class _FakeLeaseDb:
    """Records every call it receives; DbSync must forward to db.lease, not
    touch this fake's SQL surface directly."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return [("field_admin#1",)]


def _dbsync(db, ttl_seconds=1800):
    harness = Harness(id="harness-a", hostname="box", pid=1, version="0.2.0")
    return DbSync(db, harness, None, ttl_seconds=ttl_seconds)


class TestLeasePassThroughWhenEnabled:
    def test_acquire_lease_calls_db_lease_acquire(self):
        db = _FakeLeaseDb()
        assert _dbsync(db).acquire_lease("field_admin#1") is True
        assert db.calls, "must have issued a query, not short-circuited"

    def test_check_lease_calls_db_lease_check(self):
        db = _FakeLeaseDb()
        assert _dbsync(db).check_lease("field_admin#1") is True

    def test_release_expired_returns_the_freed_ids(self):
        db = _FakeLeaseDb()
        assert _dbsync(db).release_expired() == ["field_admin#1"]


class TestLeasePassThroughWhenDisabled:
    def test_acquire_lease_is_always_granted(self):
        assert _dbsync(None).acquire_lease("field_admin#1") is True

    def test_check_lease_is_always_true(self):
        assert _dbsync(None).check_lease("field_admin#1") is True

    def test_heartbeat_and_release_lease_do_not_raise(self):
        dbsync = _dbsync(None)
        dbsync.heartbeat()
        dbsync.release_lease("field_admin#1")  # must not raise

    def test_release_expired_returns_empty(self):
        assert _dbsync(None).release_expired() == []


class TestConfiguredTtlReachesDbLease:
    def test_acquire_lease_passes_the_configured_ttl_seconds_through(self):
        db = _FakeLeaseDb()
        _dbsync(db, ttl_seconds=900).acquire_lease("field_admin#1")
        _sql, params = db.calls[0]
        assert 900 in params, "the configured ttl_seconds must reach db.lease.acquire"

    def test_heartbeat_passes_the_configured_ttl_seconds_through(self):
        db = _FakeLeaseDb()
        _dbsync(db, ttl_seconds=900).heartbeat()
        _sql, params = db.calls[0]
        assert 900 in params, "the configured ttl_seconds must reach db.lease.heartbeat"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_dbsync.py -k Lease -v`
Expected: FAIL with `AttributeError: 'DbSync' object has no attribute 'acquire_lease'`

- [ ] **Step 3: Add the lease methods to `DbSync`**

Append to the existing `DbSync` class in `dbsync.py` (its constructor already
stores `self._db`, `self._harness`, `self._logger`, `self._journal` and
`self._ttl_seconds` from `__init__(self, db, harness, logger, *,
journal=None, ttl_seconds=1800)`, Task 8):

```python
    # ------------------------------------------------------------------
    # Lease operations - fail closed, NEVER journal (spec: "Claims and
    # fence checks never queue"). Each is a thin pass-through to db.lease,
    # made a no-op-but-permissive when Postgres is disabled: no shared
    # database means no second harness to coordinate with, so every lease
    # call must behave as if uncontested rather than as if blocked.
    # `ttl_seconds` (Task 8's constructor, `config.database.lease_ttl_seconds`
    # via Task 11) is threaded through acquire_lease/heartbeat here — this is
    # the only place it is ever read.
    # ------------------------------------------------------------------

    def acquire_lease(self, issue_id: str) -> bool:
        if not self.enabled:
            return True
        return db_lease.acquire(self._db, issue_id, self._harness.id, self._ttl_seconds)

    def heartbeat(self) -> None:
        if not self.enabled:
            return
        db_lease.heartbeat(self._db, self._harness.id, self._ttl_seconds)

    def release_lease(self, issue_id: str) -> None:
        if not self.enabled:
            return
        db_lease.release(self._db, issue_id, self._harness.id)

    def check_lease(self, issue_id: str) -> bool:
        if not self.enabled:
            return True
        return db_lease.check(self._db, issue_id, self._harness.id)

    def release_expired(self) -> list[str]:
        if not self.enabled:
            return []
        return db_lease.release_expired(self._db)
```

Add the import at the top of `dbsync.py`, alongside its existing `db.*` imports:

```python
from db import lease as db_lease
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_dbsync.py -v`
Expected: PASS

- [ ] **Step 5: Wire lease acquire/release into `ProcessManager`**

Modify `process_manager.py:23-49` (constructor):

```python
    def __init__(
        self,
        config: Config,
        state: StateStore,
        logger: MainLogger,
        log_queue: Queue,
        state_queue: Queue,
        dbsync: DbSync | None = None,
        harness_id: str | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._logger = logger
        self._log_queue = log_queue
        self._state_queue = state_queue
        self._color_assigner = ColorAssigner()
        # Built on first use rather than here: nothing in the constructor needs
        # GitHub, and requiring it would make ProcessManager unconstructible
        # without a fully-populated config.
        self._github: GithubClient | None = None
        # issue_id -> (Process, abort_event)
        self._workers: dict[str, tuple[Process, Event]] = {}
        # Epoch seconds until which no new workers may spawn. Rate limiting is an
        # account-wide condition, so it has to gate the whole pool — retrying a
        # single worker just burns the next slot against the same closed window.
        self._rate_limited_until: float = 0.0
        # repo -> PipelineConfig | None, cached so we do not re-read and
        # re-warn about the same pipeline.json on every spawn.
        self._pipelines: dict[str, object] = {}
        # Postgres lease system. `dbsync=None` is the pre-lease behaviour
        # exactly (see `_lease_ok`), so every existing caller that builds a
        # ProcessManager without these two new arguments is unaffected.
        self._dbsync = dbsync
        self._harness_id = harness_id
```

Add the import at the top of `process_manager.py`:

```python
from dbsync import DbSync
```

Add a new private method (place it near `can_spawn`):

```python
    def _lease_ok(self, issue_id: str) -> bool:
        """True if we may proceed spawning a worker for `issue_id`.

        Always True with no lease system wired (`self._dbsync is None`),
        matching the pre-lease behaviour exactly. Logs and returns False
        when another harness holds the lease.
        """
        if self._dbsync is None:
            return True
        if self._dbsync.acquire_lease(issue_id):
            return True
        self._logger.warn(
            f"No lease for {issue_id} — not spawning (owned by another harness)"
        )
        return False
```

Modify `process_manager.py:124-136` (top of `spawn`) to gate on it right after
the "already running" check, before any config-heavy work:

```python
    def spawn(self, record: IssueRecord) -> None:
        """Spawn a worker process for the given issue."""
        if not self.can_spawn():
            self._logger.warn(
                f"Cannot spawn worker for {record.issue_id} — at capacity "
                f"({len(self._workers)}/{self._config.workers.max_parallel})"
            )
            return

        if record.issue_id in self._workers:
            self._logger.warn(f"Worker already running for {record.issue_id}")
            return

        if not self._lease_ok(record.issue_id):
            return

        # Assign color
        color_name, color_code = self._color_assigner.assign(record.issue_id)
```

Modify the `IssueContext(...)` construction further down in `spawn` to carry
the harness id (add this line alongside the other trailing keyword arguments,
e.g. right after `pipeline_project=pipeline_project,`):

```python
            pipeline_project=pipeline_project,
            harness_id=self._harness_id,
        )
```

Modify `process_manager.py:207-234` (top of `reap_dead`) to release the lease
once a worker is confirmed gone:

```python
        for issue_id in dead:
            proc, _abort_event = self._workers.pop(issue_id)
            self._color_assigner.release(issue_id)
            proc.join(timeout=5)

            # Release now rather than waiting out the TTL: main knows for
            # certain this worker is gone, and the next spawn() re-acquires
            # a fresh lease anyway. Scoped to reap_dead() only -
            # shutdown_all()'s forced-termination path relies on TTL expiry
            # instead, which is safe (bounded by LEASE_TTL_SECONDS) if
            # slower than an explicit release.
            if self._dbsync is not None:
                self._dbsync.release_lease(issue_id)

            record = self._state.get(issue_id)
            if record is None:
                continue
```

- [ ] **Step 6: Write ProcessManager lease tests**

Append to `tests/test_process_manager_ratelimit.py`. First, extend the
existing `make_pm` helper to take the two new optional arguments (every
existing call site keeps working unchanged since both default to `None`):

```python
def make_pm(max_parallel=3, records=None, dbsync=None, harness_id=None):
    config = SimpleNamespace(
        workers=SimpleNamespace(max_parallel=max_parallel, shutdown_grace_seconds=30),
    )
    state = FakeState(records)
    logger = FakeLogger()
    return (
        ProcessManager(
            config=config,
            state=state,
            logger=logger,
            log_queue=queue.Queue(),
            state_queue=queue.Queue(),
            dbsync=dbsync,
            harness_id=harness_id,
        ),
        state,
        logger,
    )


class FakeDbSync:
    def __init__(self, grant=True):
        self.grant = grant
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire_lease(self, issue_id):
        self.acquired.append(issue_id)
        return self.grant

    def release_lease(self, issue_id):
        self.released.append(issue_id)


class TestLeaseGatesSpawn:
    """spawn() must not start a worker process without first holding the
    Postgres lease. `dbsync=None` (the default) preserves the pre-lease
    behaviour exactly, so every other test in this file is unaffected."""

    def test_no_dbsync_always_ok(self):
        pm, _, _ = make_pm()
        assert pm._lease_ok("r#1") is True

    def test_denied_lease_blocks_and_warns(self):
        dbsync = FakeDbSync(grant=False)
        pm, _, logger = make_pm(dbsync=dbsync)
        assert pm._lease_ok("r#1") is False
        assert dbsync.acquired == ["r#1"]
        assert "No lease" in logger.text()

    def test_granted_lease_allows(self):
        dbsync = FakeDbSync(grant=True)
        pm, _, _ = make_pm(dbsync=dbsync)
        assert pm._lease_ok("r#1") is True

    def test_spawn_returns_before_starting_a_process_when_lease_denied(self, monkeypatch):
        pm, _, _ = make_pm()
        pm._lease_ok = lambda issue_id: False
        calls = []
        monkeypatch.setattr("process_manager.Process", lambda **kw: calls.append(kw))
        pm.spawn(SimpleNamespace(
            issue_id="r#1", repo="r", number=1, title="t", body="",
            action="fix", branch=None, pr_url=None, rework_count=0,
            handoff_summary=None,
        ))
        assert calls == [], "must not construct a worker Process without the lease"


class TestLeaseReleaseOnReap:
    def test_releases_the_lease_when_a_worker_is_reaped(self):
        rec = SimpleNamespace(issue_id="r#1", status="completed", error=None,
                               continuation_count=0, branch=None, number=1, repo="r")
        dbsync = FakeDbSync()
        pm, _, _ = make_pm(records={"r#1": rec}, dbsync=dbsync)
        pm._workers["r#1"] = (FakeProc(exitcode=0), object())

        pm.reap_dead()

        assert dbsync.released == ["r#1"]

    def test_no_dbsync_reap_does_not_error(self):
        rec = SimpleNamespace(issue_id="r#1", status="completed", error=None,
                               continuation_count=0, branch=None, number=1, repo="r")
        pm, _, _ = make_pm(records={"r#1": rec})
        pm._workers["r#1"] = (FakeProc(exitcode=0), object())

        pm.reap_dead()  # must not raise
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_process_manager_ratelimit.py -v`
Expected: PASS (all existing tests still pass unmodified, plus 6 new)

- [ ] **Step 8: Write the failing test for heartbeat cadence**

```python
"""Tests for main._maybe_heartbeat - the lease heartbeat's cadence.

Guards docs/plans/12-shared-state-in-postgres.md, "main owns the heartbeat":
the poll loop's shutdown-responsive sleep runs in 1-second increments, but a
slow poll/triage pass can spend many seconds without ever reaching that
sleep loop, so heartbeat cadence must be driven by elapsed wall clock and
called from more than one point in main(), not by "did the sleep loop run
N times".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


class _FakeDbSync:
    def __init__(self):
        self.heartbeats = 0

    def heartbeat(self):
        self.heartbeats += 1


class TestMaybeHeartbeat:
    def test_does_not_fire_before_the_interval_elapses(self):
        dbsync = _FakeDbSync()
        last = main._maybe_heartbeat(dbsync, last_at=100.0, interval=60, now=159.0)
        assert dbsync.heartbeats == 0
        assert last == 100.0, "unchanged last_at is how the next call knows nothing fired"

    def test_fires_once_the_interval_has_elapsed(self):
        dbsync = _FakeDbSync()
        last = main._maybe_heartbeat(dbsync, last_at=100.0, interval=60, now=161.0)
        assert dbsync.heartbeats == 1
        assert last == 161.0

    def test_a_slow_pass_that_never_reaches_the_sleep_loop_still_heartbeats(self):
        # Simulates step 1 of the poll loop (drain/reap) calling this
        # directly, not just the per-second sleep tick - see main.py's two
        # call sites.
        dbsync = _FakeDbSync()
        last = 0.0
        for elapsed in (10.0, 200.0):  # one slow pass alone blows past 60s
            last = main._maybe_heartbeat(dbsync, last_at=last, interval=60, now=elapsed)
        assert dbsync.heartbeats == 1
```

- [ ] **Step 9: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_heartbeat.py -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute '_maybe_heartbeat'`

- [ ] **Step 10: Implement `_maybe_heartbeat` and wire it into `main`**

Add near `_release_stale_locks` in `main.py`:

```python
def _maybe_heartbeat(dbsync, last_at: float, interval: float, now: float | None = None) -> float:
    """Call `dbsync.heartbeat()` if `interval` seconds have passed since `last_at`.

    Returns the timestamp to use as `last_at` on the next call. `now`
    defaults to `time.monotonic()` and exists purely so tests do not have to
    sleep for real — production callers never pass it.

    Called from both the top of the poll loop and its per-second sleep tick
    (see `main`), because heartbeat cadence must not depend on the sleep
    loop running uninterrupted — a slow poll/triage pass that never reaches
    the sleep loop must still keep leases alive.
    """
    now = time.monotonic() if now is None else now
    if now - last_at < interval:
        return last_at
    dbsync.heartbeat()
    return now
```

Modify `main.py:492-498` (ProcessManager construction) — this assumes `db`,
`harness`, and `dbsync` already exist as local variables in `main()` from an
earlier phase's wiring:

```python
    process_manager = ProcessManager(
        config=config,
        state=state,
        logger=logger,
        log_queue=log_queue,
        state_queue=state_queue,
        dbsync=dbsync,
        harness_id=harness.id,
    )
```

Modify `main.py` just above the polling `while` loop (~line 528) to seed the
cadence tracker:

```python
    last_heartbeat = time.monotonic()

    try:
        while not shutdown_requested:
            # 1. Drain queues + reap dead workers
            process_manager.drain_state_queue()
            logger.drain_queue(log_queue)
            process_manager.reap_dead()
            last_heartbeat = _maybe_heartbeat(
                dbsync, last_heartbeat, config.database.heartbeat_interval_seconds
            )
```

Modify the per-second sleep loop (`main.py:574-580`):

```python
            # 7. Sleep in small increments so shutdown is responsive
            for _ in range(config.github.poll_interval_seconds):
                if shutdown_requested:
                    break
                # Drain queues during sleep too
                process_manager.drain_state_queue()
                logger.drain_queue(log_queue)
                last_heartbeat = _maybe_heartbeat(
                    dbsync, last_heartbeat, config.database.heartbeat_interval_seconds
                )
                time.sleep(1)
```

- [ ] **Step 11: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_heartbeat.py -v`
Expected: PASS (3 passed)

- [ ] **Step 12: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 598 passed, 2 skipped, 0 failed

- [ ] **Step 13: Commit**

```bash
git add dbsync.py process_manager.py main.py tests/test_dbsync.py tests/test_process_manager_ratelimit.py tests/test_heartbeat.py
git commit -m "feat(lease): acquire before spawn, heartbeat from main, release on reap (ai-cc)"
```

---

### Task 14: worker-side fencing

**Files:**
- Modify: `worker.py:16-28` (imports), `:82-127` (`IssueContext`),
  `:826-909` (`_push_partial_work`), `:1149-1212` (`_push_rework`),
  `:1429-1452` (`_set_labels`), `:1498-1577` (`_push_and_pr`),
  `:1584-1868` (`run_dev_worker`), `:2022-2232` (`_post_pr_review`,
  `run_review_worker`)
- Test: `tests/test_worker_fencing.py`

**Interfaces:**
- Consumes: Task 12's `db.lease` (transitively, via `DbSync.check_lease`),
  Task 13's `dbsync.DbSync`, `db.pool.Database`, `db.harness.Harness`
  (Task 6), and `integrations.METRICS_DB_ENV_VAR` (already used by `main.py`
  as the name of the env var holding the Postgres URL). `db/journal.py` does
  not exist until Task 20 and is not needed here — `DbSync` is constructed
  below with no `journal` argument (Task 8's default of `None`), which is
  fine because `check_lease` never journals in the first place.
- Produces (consumed by nothing later in this phase; `LeaseLostError` and
  `IssueContext.harness_id` are part of the frozen worker surface going
  forward):
  ```python
  class LeaseLostError(RuntimeError): ...
  def _assert_lease_held(ctx: IssueContext, logger: WorkerLogger) -> None
  # IssueContext gains: harness_id: str | None = None
  ```

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for lease fencing - worker.py's `_assert_lease_held` and its wiring
into every irreversible remote act (git push, gh pr create, gh pr review,
ac-* label writes).

Guards docs/plans/12-shared-state-in-postgres.md, "Fencing: never kill a
running agent": if this box's lease expires mid-run, another harness may
legitimately retake the issue while the local agent keeps running. The agent
is never aborted here - it is the *acts after it finishes* that must be
refused, the same way `assert_pushable`/`ProtectedBranchError` already
refuse a push to the wrong branch. Two layers: (1) `_assert_lease_held`'s
own decision logic against a fake DbSync, and (2) that every guarded
function actually calls it before touching the remote, mirroring
tests/test_push_guard.py's wiring tests.
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402
from worker import LeaseLostError  # noqa: E402


def _ctx(tmp_path, harness_id="harness-a"):
    return worker.IssueContext(
        issue_id="field_admin#215",
        repo="field_admin",
        number=215,
        title="Job wizard progress",
        body="",
        action="implement",
        org="Accelevation",
        base_branch="dev",
        repos_dir=tmp_path,
        worktrees_dir=tmp_path,
        prompts_dir=tmp_path,
        dev_model="opus",
        light_model="sonnet",
        permission_mode="acceptEdits",
        max_budget_usd=10.0,
        max_turns=100,
        crash_logs_dir=tmp_path,
        color_name="blue",
        color_code="\033[34m",
        harness_id=harness_id,
    )


def _logger():
    return SimpleNamespace(
        info=lambda *_a, **_k: None,
        warn=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )


# ---------------------------------------------------------------------------
# Layer 1: _assert_lease_held's own decision logic
# ---------------------------------------------------------------------------

class _FakeDbSync:
    def __init__(self, held: bool):
        self._held = held
        self.checked: list[str] = []

    def check_lease(self, issue_id):
        self.checked.append(issue_id)
        return self._held


class TestAssertLeaseHeld:
    def test_raises_when_the_lease_is_lost(self, tmp_path, monkeypatch):
        monkeypatch.setattr(worker, "DbSync", lambda *a, **k: _FakeDbSync(held=False))
        monkeypatch.setattr(worker, "Database", lambda *a, **k: SimpleNamespace(close=lambda: None))
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgres://fake/db")
        with pytest.raises(LeaseLostError):
            worker._assert_lease_held(_ctx(tmp_path), _logger())

    def test_does_not_raise_when_the_lease_is_held(self, tmp_path, monkeypatch):
        monkeypatch.setattr(worker, "DbSync", lambda *a, **k: _FakeDbSync(held=True))
        monkeypatch.setattr(worker, "Database", lambda *a, **k: SimpleNamespace(close=lambda: None))
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgres://fake/db")
        worker._assert_lease_held(_ctx(tmp_path), _logger())  # must not raise

    def test_no_op_when_no_shared_database_is_configured(self, tmp_path, monkeypatch):
        # No shared database means no second harness to fence against - see
        # main._release_stale_locks's degraded-path reasoning, applied here
        # mid-run instead of at startup.
        monkeypatch.delenv("PIPELINE_METRICS_DATABASE_URL", raising=False)
        worker._assert_lease_held(_ctx(tmp_path), _logger())  # must not raise

    def test_no_op_when_the_context_carries_no_harness_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgres://fake/db")
        worker._assert_lease_held(_ctx(tmp_path, harness_id=None), _logger())


# ---------------------------------------------------------------------------
# Layer 2: wiring into every irreversible act
# ---------------------------------------------------------------------------

class _FakeResult:
    """Reports a dirty tree so the commit path runs, and success on
    everything else, so nothing but the fence can stop a push/PR/label."""
    returncode = 0
    stdout = " M src/foo.ts\n"
    stderr = ""


def _record_cmds(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **_kwargs):
        calls.append(list(cmd))
        return _FakeResult()

    monkeypatch.setattr(worker, "_run_cmd", fake_run_cmd)
    return calls


def _fence(monkeypatch, *, lost=False, raise_on_call=None):
    """Replace worker._assert_lease_held with a canned verdict. `raise_on_call`
    (1-indexed) makes only that specific call raise, simulating a lease lost
    partway through a function that touches the remote more than once (push,
    then PR create). Returns the call counter."""
    calls = {"n": 0}

    def fake(ctx, logger):
        calls["n"] += 1
        if lost and (raise_on_call is None or calls["n"] == raise_on_call):
            raise LeaseLostError(f"lease lost (test, call {calls['n']})")

    monkeypatch.setattr(worker, "_assert_lease_held", fake)
    return calls


class TestPushAndPrIsFenced:
    def test_lease_lost_before_the_push_blocks_everything(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True, raise_on_call=1)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_and_pr(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert calls == [], "no git/gh call may happen once the fence has fired"

    def test_lease_lost_between_push_and_pr_create_blocks_only_the_pr(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True, raise_on_call=2)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_and_pr(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert any(c[:2] == ["git", "push"] for c in calls), "the push already happened"
        assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)

    def test_lease_held_pushes_and_opens_the_pr(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        worker._push_and_pr(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert any(c[:2] == ["git", "push"] for c in calls)
        assert any(c[:3] == ["gh", "pr", "create"] for c in calls)


class TestPushReworkIsFenced:
    def test_lease_lost_blocks_the_push(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_rework(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert calls == []

    def test_lease_held_still_pushes(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        worker._push_rework(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert any(c[:2] == ["git", "push"] for c in calls)


class TestPushPartialWorkIsFenced:
    def test_lease_lost_before_the_push_propagates(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True, raise_on_call=1)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_partial_work(_ctx(tmp_path), "ac/issue-215-x", tmp_path, _logger())
        assert calls == [], "no git/gh call may happen once the fence has fired"

    def test_lease_lost_between_push_and_pr_create_blocks_only_the_pr(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True, raise_on_call=2)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_partial_work(_ctx(tmp_path), "ac/issue-215-x", tmp_path, _logger())
        assert any(c[:2] == ["git", "push"] for c in calls)
        assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)

    def test_lease_held_pushes_and_opens_the_wip_pr(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        worker._push_partial_work(_ctx(tmp_path), "ac/issue-215-x", tmp_path, _logger())
        assert any(c[:2] == ["git", "push"] for c in calls)
        assert any(c[:3] == ["gh", "pr", "create"] for c in calls)


class TestSetLabelsIsFenced:
    def test_lease_lost_blocks_every_label_write(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._set_labels(_ctx(tmp_path), _logger(),
                                add=["ac-dev-review"], remove=["ac-in-progress"])
        assert calls == []

    def test_lease_held_writes_labels(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        worker._set_labels(_ctx(tmp_path), _logger(),
                            add=["ac-dev-review"], remove=["ac-in-progress"])
        assert any(c[:3] == ["gh", "issue", "edit"] for c in calls)

    def test_a_no_op_call_never_checks_the_lease(self, monkeypatch, tmp_path):
        checked = _fence(monkeypatch, lost=False)
        worker._set_labels(_ctx(tmp_path), _logger())  # nothing to add/remove
        assert checked["n"] == 0


class TestPostPrReviewIsFenced:
    def test_lease_lost_blocks_the_review(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True)
        calls = _record_cmds(monkeypatch)
        ctx = _ctx(tmp_path)
        ctx.pr_url = "https://github.com/Accelevation/field_admin/pull/42"
        with pytest.raises(LeaseLostError):
            worker._post_pr_review(ctx, _logger(), approve=True, body="ok")
        assert calls == []

    def test_lease_held_posts_the_review(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        ctx = _ctx(tmp_path)
        ctx.pr_url = "https://github.com/Accelevation/field_admin/pull/42"
        worker._post_pr_review(ctx, _logger(), approve=True, body="ok")
        assert any(c[:3] == ["gh", "pr", "review"] for c in calls)


# ---------------------------------------------------------------------------
# Layer 3: the fenced exit path
# ---------------------------------------------------------------------------

class TestHandleLeaseLost:
    def test_sends_a_fenced_state_update_writes_a_crash_log_and_touches_no_remote(
        self, monkeypatch, tmp_path,
    ):
        posted = []
        monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: posted.append(a))
        remote_calls = _record_cmds(monkeypatch)
        q = queue.Queue()
        ctx = _ctx(tmp_path)
        ctx.pr_url = "https://github.com/Accelevation/field_admin/pull/42"

        worker._handle_lease_lost(
            ctx, _logger(), q, LeaseLostError("lease lost"), branch="ac/issue-215-x",
        )

        update = q.get_nowait()
        assert update.status == "failed"
        assert update.error.startswith("fenced:")
        assert update.branch == "ac/issue-215-x"
        assert update.pr_url == ctx.pr_url
        assert posted == [], "must never post a crash comment - that touches the remote"
        assert remote_calls == []
        assert len(list(tmp_path.glob("*.log"))) == 1, "a local crash log must still be written"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_worker_fencing.py -v`
Expected: FAIL with `ImportError: cannot import name 'LeaseLostError' from 'worker'`

- [ ] **Step 3: Add the guard machinery**

Modify `worker.py:16-28` — extend the import block:

```python
import stages
from db.harness import Harness
from db.pool import Database
from dbsync import DbSync
from ghauth import apply_git_credentials, build_env, current_token
from integrations import METRICS_DB_ENV_VAR, TelemetryEvent, log_event
from logger import WorkerLogger
from pipeline import load_pipeline_config
from ratelimit import (
    RateLimitInfo,
    detect_rate_limit_in_stderr,
    parse_rate_limit_event,
    pause_seconds,
)
from redact import redact
from worktree_setup import RepoSetupConfig, prepare_worktree
```

Add the guard, right after `assert_pushable` (`worker.py`, end of the "Push
guard" section, ~line 80):

```python
class LeaseLostError(RuntimeError):
    """Raised when this harness no longer holds the Postgres lease on the
    issue being worked, discovered immediately before an irreversible remote
    act. Mirrors `ProtectedBranchError`: fails LOUD by raising, so a caller
    cannot forget to check a return value.
    """


def _assert_lease_held(ctx: IssueContext, logger: WorkerLogger) -> None:
    """Refuse an irreversible act once another harness owns this issue.

    Called immediately before every `git push`, `gh pr create`, `gh pr
    review`, and inside `_set_labels` - the single chokepoint for every
    `ac-*` label write - mirroring where `assert_pushable` is called. A
    no-op when `ctx.harness_id` is unset or no shared database is configured
    (`PIPELINE_METRICS_DATABASE_URL`), because both mean "no shared
    database, no second harness to fence against" - see
    `main._release_stale_locks`'s degraded-path reasoning, applied here
    mid-run instead of at startup.

    Builds its own `Database`/`DbSync` from `os.environ` rather than
    receiving one from `main`: the worker is a separate `spawn`ed process,
    so a live connection cannot cross the pickle boundary. Only
    `check_lease` is ever called on the result, which per its docstring
    fails CLOSED (returns False, i.e. "lease lost") if Postgres is
    unreachable after its own internal retries - so this function needs no
    second layer of retry logic.
    """
    if not ctx.harness_id:
        return
    url = os.environ.get(METRICS_DB_ENV_VAR)
    if not url:
        return

    db = Database(url)
    try:
        harness = Harness(id=ctx.harness_id, hostname="", pid=0, version="")
        # No `journal` passed — Task 8's default of `None` is fine here,
        # since check_lease never journals in the first place (spec:
        # "Claims and fence checks never queue").
        dbsync = DbSync(db, harness, logger)
        if not dbsync.check_lease(ctx.issue_id):
            raise LeaseLostError(
                f"Lease on {ctx.issue_id} is no longer held by harness "
                f"{ctx.harness_id} — refusing to touch the remote."
            )
    finally:
        db.close()


def _handle_lease_lost(
    ctx: IssueContext,
    logger: WorkerLogger,
    state_queue: Queue,
    exc: LeaseLostError,
    *,
    branch: str | None = None,
) -> None:
    """Common fenced-exit path shared by run_dev_worker and run_review_worker.

    Writes a local crash log (disk only, never the remote) and sends a
    StateUpdate whose `error` is prefixed "fenced:" — StateUpdate's shape is
    frozen for Phases A-C, so this is how a later phase's run/summary
    capture can recognise and record a `summary` row with kind="fenced"
    without a new field. Deliberately does NOT call `_post_crash_comment`
    (a GitHub comment is exactly the remote touch fencing exists to
    prevent) and does NOT clean up the worktree — the branch stays local
    and the issue is retried by whichever harness now holds it.
    """
    logger.error(f"Fenced: {exc}")
    log_path = _write_crash_log(ctx, str(exc), logger)
    logger.warn(f"Lease lost — leaving remote state untouched; crash log at {log_path}")
    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="failed",
        error=f"fenced: {exc}",
        branch=branch,
        pr_url=ctx.pr_url,
    ))
```

- [ ] **Step 4: Add `harness_id` to `IssueContext`**

Modify `worker.py:82-127` — add one field at the end of the dataclass (every
field after it already has a default, so appending here is safe):

```python
    # Worktree-preparation overrides from `[repos.<name>]`. None means
    # auto-detect from what is in the checkout, which is the normal case.
    repo_setup: RepoSetupConfig | None = None
    # Which Postgres harness row spawned this worker. Set by
    # ProcessManager.spawn from its own `harness_id`; None when no lease
    # system is wired (--issue mode without Postgres, or an older caller) -
    # `_assert_lease_held` treats that the same as "no shared database".
    harness_id: str | None = None
```

- [ ] **Step 5: Run the test to verify Layer 1 passes**

Run: `.venv\Scripts\python -m pytest tests/test_worker_fencing.py::TestAssertLeaseHeld -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Wire the fence into every irreversible act**

Modify `worker.py:826-909` (`_push_partial_work`) — add the fence right
after the existing branch guard, and again right before the PR-create block:

```python
def _push_partial_work(
    ctx: IssueContext,
    branch: str,
    worktree_dir: Path,
    logger: WorkerLogger,
) -> str | None:
    """Commit and push partial work after budget exhaustion. Returns pr_url or None."""
    # This path is best-effort by contract — every other failure here returns
    # None rather than raising, so the guard does the same.
    try:
        assert_pushable(branch, ctx.base_branch)
    except ProtectedBranchError as exc:
        logger.error(str(exc))
        return None
    _assert_lease_held(ctx, logger)

    # Check for uncommitted changes
    status_result = _run_cmd(
        ["git", "status", "--porcelain"],
        cwd=worktree_dir,
        logger=logger,
    )
    has_uncommitted = bool(status_result.stdout.strip())

    # ... [unchanged: has_commits check, commit-if-uncommitted block] ...

    logger.info(f"Pushing partial work to branch {branch}...")
    result = _run_cmd(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree_dir,
        logger=logger,
        timeout=60,
    )
    if result.returncode != 0:
        logger.warn(f"Partial push failed: {result.stderr.strip()}")
        return None

    # Create PR if one doesn't exist yet
    if not ctx.pr_url:
        _assert_lease_held(ctx, logger)
        pr_body = f"Work in progress — budget exceeded, continuation pending.\n\nAddresses #{ctx.number}"
        pr_title = f"wip: {ctx.title} (#{ctx.number})"
        logger.info("Creating WIP pull request...")
        result = _run_cmd(
            [
                "gh", "pr", "create",
                "--repo", f"{ctx.org}/{ctx.repo}",
                "--title", pr_title,
                "--body", pr_body,
                "--head", branch,
                "--base", ctx.base_branch,
                "--draft",
            ],
            logger=logger,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()

    return ctx.pr_url
```

(`# ... [unchanged: ...] ...` marks the block between the two shown hunks
that is not touched — the real edit is two single-line insertions into the
existing function body, not a rewrite of it.)

Modify `worker.py:1149-1212` (`_push_rework`) — one insertion, right after
`assert_pushable`:

```python
    """Commit and push rework changes to the existing branch. Returns pr_url."""
    # `branch` here came from local state or a PR's headRefName — not from us.
    # Fail before staging anything.
    assert_pushable(branch, ctx.base_branch)
    _assert_lease_held(ctx, logger)
```

Modify `worker.py:1429-1452` (`_set_labels`) — the single chokepoint for
every `ac-*` label write, so this ONE insertion covers `_claim_labels`,
`_success_labels`, `_failure_labels`, `_claim_review_labels`,
`_review_pass_labels`, `_review_fail_labels`, and
`_release_review_lock_after_crash`:

```python
def _set_labels(
    ctx: IssueContext,
    logger: WorkerLogger,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """Add and/or remove labels on the issue in a single gh call.

    The chokepoint for every `ac-*` label write - `_assert_lease_held` runs
    immediately before the `gh issue edit` call, not at the top of the
    function, so a no-op call (nothing to add or remove) never pays for a
    lease check it does not need.
    """
    args = [
        "gh", "issue", "edit", str(ctx.number),
        "--repo", f"{ctx.org}/{ctx.repo}",
    ]
    if add:
        args += ["--add-label", ",".join(add)]
    if remove:
        args += ["--remove-label", ",".join(remove)]

    if add or remove:
        _assert_lease_held(ctx, logger)
        desc = []
        if remove:
            desc.append(f"-{','.join(remove)}")
        if add:
            desc.append(f"+{','.join(add)}")
        logger.info(f"Labels: {' '.join(desc)}")
        _run_cmd(args, logger=logger, timeout=30)
```

Modify `worker.py:1498-1577` (`_push_and_pr`) — two insertions, mirroring
`_push_partial_work`:

```python
def _push_and_pr(
    ctx: IssueContext,
    branch: str,
    worktree_dir: Path,
    summary: str,
    logger: WorkerLogger,
) -> str:
    """Commit, push, create PR, comment on issue. Returns PR URL."""
    assert_pushable(branch, ctx.base_branch)
    _assert_lease_held(ctx, logger)

    # ... [unchanged: commit-if-uncommitted block] ...

    # Push
    logger.info(f"Pushing branch {branch}...")
    result = _run_cmd(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree_dir,
        logger=logger,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Push failed: {result.stderr.strip()}")

    _assert_lease_held(ctx, logger)

    # Create PR — redact summary to avoid leaking secrets from Claude output
    pr_body = f"{redact(summary)}\n\nCloses #{ctx.number}" if summary else f"Closes #{ctx.number}"
    pr_title = f"{ctx.action}: {ctx.title} (#{ctx.number})"
    logger.info("Creating pull request...")
    result = _run_cmd(
        [
            "gh", "pr", "create",
            "--repo", f"{ctx.org}/{ctx.repo}",
            "--title", pr_title,
            "--body", pr_body,
            "--head", branch,
            "--base", ctx.base_branch,
        ],
        logger=logger,
        timeout=30,
    )
    # ... [unchanged: rest of the function] ...
```

Modify `worker.py:2022-2040` (`_post_pr_review`):

```python
def _post_pr_review(
    ctx: IssueContext,
    logger: WorkerLogger,
    *,
    approve: bool,
    body: str,
) -> None:
    """Post an approving or changes-requested review on the PR via gh."""
    pr_number = _pr_number(ctx.pr_url)
    if pr_number is None:
        logger.warn("No PR number to review — skipping gh pr review")
        return
    _assert_lease_held(ctx, logger)
    args = [
        "gh", "pr", "review", str(pr_number),
        "--repo", f"{ctx.org}/{ctx.repo}",
        "--approve" if approve else "--request-changes",
        "--body", redact(body),
    ]
    _run_cmd(args, logger=logger, timeout=30)
```

- [ ] **Step 7: Run the test to verify Layer 2 passes**

Run: `.venv\Scripts\python -m pytest tests/test_worker_fencing.py -v`
Expected: PASS (all but `TestHandleLeaseLost`, which needs Step 8)

- [ ] **Step 8: Wire `LeaseLostError` into both worker entry points**

Modify `worker.py:1602-1615` (top of `run_dev_worker`) — move the self-lock
inside the `try` so a lease already lost by spawn time (an extreme race,
since `main` only just acquired it in Task 13) is fenced like every other
irreversible act rather than crashing the process unhandled:

```python
    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="in_progress",
        worker_pid=pid,
    ))

    branch = sanitize_branch_name(ctx.title, ctx.number)
    worktree_dir = ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}"
    is_fresh_branch = False  # True if rework fell back to a new versioned branch

    try:
        # [0] Self-lock. Inside the try so a lease lost between spawn and
        # here is fenced, not an unhandled crash.
        _claim_labels(ctx, logger)

        # [1] Clone / fetch
        repo_dir = _clone_or_fetch(ctx, logger)
```

Modify `worker.py:1833-1868` (the `except` block at the end of
`run_dev_worker`) — add a new clause for `LeaseLostError`, listed BEFORE the
existing `except Exception`, since it is a subclass and Python matches the
first `except` clause that fits:

```python
    except LeaseLostError as exc:
        _handle_lease_lost(ctx, logger, state_queue, exc, branch=branch)

    except Exception as exc:
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Worker failed: {exc}")

        # Write crash log and post comment
        log_path = _write_crash_log(ctx, error_detail, logger)
        _post_crash_comment(ctx, str(exc), log_path, logger)

        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error=str(exc),
        ))

        # ... [unchanged: bump attempt counter, worktree cleanup] ...
```

Modify `worker.py:2059-2070` (top of `run_review_worker`) — same move:

```python
    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="in_progress",
        worker_pid=pid,
    ))

    worktree_dir = ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}-review"
    repo_dir = ctx.repos_dir / ctx.repo

    try:
        # [1] Self-lock FIRST, before any expensive work, so a concurrent
        # runner cannot double-claim this review. Inside the try so a lease
        # lost between spawn and here is fenced, not an unhandled crash.
        _claim_review_labels(ctx, logger)

        # `ctx.pr_url`/`existing_branch` are only populated when this same
```

Modify `worker.py:2200-2232` (the `except` block at the end of
`run_review_worker`) — same pattern:

```python
    except LeaseLostError as exc:
        _handle_lease_lost(ctx, logger, state_queue, exc)

    except Exception as exc:
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Review worker failed: {exc}")

        # ... [unchanged: rest of the existing except block] ...
```

- [ ] **Step 9: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_worker_fencing.py -v`
Expected: PASS (all tests, including `TestHandleLeaseLost`)

- [ ] **Step 10: Confirm the push-guard meta-test still holds, explicitly**

Run: `.venv\Scripts\python -m pytest tests/test_push_guard.py -v`
Expected: PASS. The arithmetic
(`tests/test_push_guard.py:200-210`) is unaffected by this task:
`source.count('"git", "push"')` is still **3** (the literal `git push`
argv fragments in `_push_partial_work`, `_push_rework`, `_push_and_pr` are
untouched — no new push call site was added), and
`source.count("assert_pushable(")` is still **4** (the definition plus the
same three existing call sites — `_assert_lease_held` is a distinct
function name and is never counted). `4 - 1 = 3 >= 3` still holds. Every
`_assert_lease_held(` call added by this task (7 of them: two in
`_push_partial_work`, one in `_push_rework`, two in `_push_and_pr`, one in
`_set_labels`, one in `_post_pr_review`) is invisible to this specific
meta-test by construction, since it only counts the two literal substrings
above.

- [ ] **Step 11: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 616 passed, 2 skipped, 0 failed

- [ ] **Step 12: Commit**

```bash
git add worker.py tests/test_worker_fencing.py
git commit -m "feat(lease): fence every push, PR, review and label write on lost lease (ai-cc)"
```

---

### Task 15: replace `_release_stale_locks`

**Files:**
- Modify: `main.py:14-31` (imports), `:122-165` (`_release_stale_locks`),
  `:510` (call site)
- Test: `tests/test_wiring.py` (append to `TestReleaseStaleLocks`)

**Interfaces:**
- Consumes: Task 12's `db.lease.release_expired`, `db.issue_state.fetch`
  (from an earlier phase; frozen shape: `dict | None` with
  `owner_harness_id`, `lease_expires_at` among its keys), `db.pool.DbUnavailable`,
  `pipeline.parse_pipeline_config`/`PipelineConfigError`, and `db.lease.LEASE_TTL_SECONDS`.
- Produces: `main._release_stale_locks` gains a new optional `db` parameter;
  every other caller/signature in this task is private to `main.py`.
  ```python
  def _release_stale_locks(config, github, logger, dry_run: bool = False, db=None) -> int
  ```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_wiring.py`, inside/after the existing
`TestReleaseStaleLocks` class. **No existing test in that class needs to
change** — `db` defaults to `None`, which is exactly the old,
always-instantly-stale behaviour every current test already exercises. The
new tests below cover the Postgres-aware path.

```python
class _FakeDbUnavailable(Exception):
    """Stand-in for db.pool.DbUnavailable that does not require importing
    the real db package into this test file."""


class FakeLeaseDb:
    """Stands in for a real db.pool.Database, driven entirely through
    main.db_lease / main.db_issue_state monkeypatches below - this class
    itself is never called by _release_stale_locks directly, it just needs
    to be a non-None sentinel."""


def _patch_lease(monkeypatch, *, rows=None, release_expired_raises=False,
                  fetch_raises_for=()):
    """Monkeypatch main.db_lease.release_expired and main.db_issue_state.fetch.

    `rows`: issue_id -> {"owner_harness_id": str | None}. Missing issue_id
    means fetch() returns None (no Postgres row at all - e.g. a pre-Postgres
    holdover), which must be treated as free.
    """
    rows = rows or {}

    def fake_release_expired(db):
        if release_expired_raises:
            raise _FakeDbUnavailable("release_expired: db down")
        return []

    def fake_fetch(db, issue_id):
        if issue_id in fetch_raises_for:
            raise _FakeDbUnavailable(f"fetch({issue_id}): db down")
        return rows.get(issue_id)

    monkeypatch.setattr(main.db_lease, "release_expired", fake_release_expired)
    monkeypatch.setattr(main.db_issue_state, "fetch", fake_fetch)
    monkeypatch.setattr(main, "DbUnavailable", _FakeDbUnavailable)


class TestReleaseStaleLocksWithLease:
    def test_rewinds_a_lock_whose_lease_is_free(self, monkeypatch):
        _patch_lease(monkeypatch, rows={"field_admin#7": {"owner_harness_id": None}})
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress", "ac-fix"])]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 1
        assert ("field_admin", 7, "ac-dev-ready") in gh.added
        assert ("field_admin", 7, "ac-in-progress") in gh.removed

    def test_leaves_a_lock_whose_lease_is_held_by_another_harness(self, monkeypatch):
        _patch_lease(monkeypatch, rows={
            "field_admin#7": {"owner_harness_id": "some-other-harness"},
        })
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress", "ac-fix"])]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 0
        assert not gh.added and not gh.removed

    def test_treats_a_missing_postgres_row_as_free(self, monkeypatch):
        # A pre-Postgres holdover, or a row this harness never got to upsert.
        _patch_lease(monkeypatch, rows={})
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress"])]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 1

    def test_release_expired_failing_at_startup_rewinds_nothing(self, monkeypatch):
        # Fails closed: cannot verify safety, so touch nothing this run,
        # rather than falling back to "assume everything is stale".
        _patch_lease(monkeypatch, release_expired_raises=True,
                      rows={"field_admin#7": {"owner_harness_id": None}})
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress"])]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 0
        assert not gh.added and not gh.removed

    def test_a_fetch_failure_mid_sweep_leaves_that_one_issue_alone(self, monkeypatch):
        _patch_lease(monkeypatch, fetch_raises_for={"field_admin#7"},
                      rows={"field_admin#8": {"owner_harness_id": None}})
        gh = FakeGithub({"field_admin": [
            _issue(7, ["ac-in-progress"]),
            _issue(8, ["ac-in-progress"]),
        ]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 1
        assert ("field_admin", 8, "ac-dev-ready") in gh.added
        assert not any(num == 7 for _r, num, _l in gh.added)

    def test_no_db_argument_still_behaves_like_before(self):
        # db is None by default - the degraded/no-Postgres path, unchanged.
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress"])]})
        assert main._release_stale_locks(_config(), gh, _logger()) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_wiring.py::TestReleaseStaleLocksWithLease -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute 'db_lease'`

- [ ] **Step 3: Write the implementation**

Modify `main.py:14-31` — extend the `pipeline` import and add the new
Postgres-facing imports:

```python
import stages
from config import load_config
from db import issue_state as db_issue_state
from db import lease as db_lease
from db.pool import DbUnavailable
from ghauth import (
    TOKEN_ENV_VAR,
    load_dotenv,
    check_access,
    check_ownership_config,
    format_report,
    has_fatal,
    load_token,
    verify_identity,
)
from github_client import GithubClient, GithubClientError
from integrations import METRICS_DB_ENV_VAR, sync_board
from logger import MainLogger, enable_ansi_windows
from pipeline import PIPELINE_JSON_RELATIVE_PATH, PipelineConfigError, parse_pipeline_config
from poller import Poller
from process_manager import ProcessManager
from state import IssueStatus, StateStore
from triage import TriageEngine, format_clarifying_comment
```

Replace `main.py:122-165` (`_release_stale_locks`) in full:

```python
def _release_stale_locks(config, github: GithubClient, logger: MainLogger,
                         dry_run: bool = False, db=None) -> int:
    """Rewind locks left behind by a crashed or killed run.

    Expiry-driven, not startup-driven: this can no longer assume no worker
    of ours is alive elsewhere - a second harness may hold a perfectly live
    lease on an issue this box also sees carrying `ac-in-progress`. A label
    is only rewound once its Postgres lease is confirmed free or expired
    (see docs/plans/12-shared-state-in-postgres.md, "The blocker this
    removes").

    `db` is the raw Postgres handle (`db.pool.Database`), not `DbSync` -
    this is a one-time startup sweep across every repo's issues, not a
    per-issue write on the worker hot path, so it goes straight at
    `db.lease`/`db.issue_state` rather than through the single-writer seam
    those modules exist to protect.

    Degraded path: `db is None` means Postgres is disabled, or was
    unreachable when `main` tried to construct it - in which case there
    cannot be a second harness (no shared database to coordinate through)
    and every bot-assigned locked issue is stale by definition, exactly as
    before this task. If Postgres *is* configured but `release_expired`
    fails right here (a transient outage), this fails CLOSED instead of
    falling back to that assumption: nothing is rewound for the rest of
    this call, because a configured-but-currently-unreachable database
    might still have a live second harness we simply cannot see, and
    rewinding blind is the exact bug this function exists to prevent.
    """
    lease_verified = db is not None
    if lease_verified:
        try:
            freed = db_lease.release_expired(db)
            if freed:
                logger.info(f"Postgres: {len(freed)} expired lease(s) released")
        except DbUnavailable as exc:
            logger.warn(
                f"Postgres unreachable at startup — cannot verify which "
                f"locks are genuinely stale, so none will be rewound this "
                f"run: {exc}"
            )
            lease_verified = False

    released = 0
    for repo in config.github.repos:
        if lease_verified:
            _warn_stale_lock_hours(config, github, repo, logger)

        try:
            issues = github.list_issues(repo, assignee=config.github.bot_login)
        except GithubClientError as exc:
            logger.warn(f"Could not check {repo} for stale locks: {exc}")
            continue

        for issue in issues:
            labels = [lbl["name"] for lbl in issue.get("labels", [])]
            target = stages.stale_reset_target(labels)
            if target is None:
                continue

            issue_id = f"{repo}#{issue['number']}"

            if db is None:
                pass  # degraded path: no shared database, no second harness
            elif not lease_verified:
                logger.info(f"{issue_id}: cannot verify lease — leaving it")
                continue
            elif not _lease_is_free(db, issue_id, logger):
                logger.info(f"{issue_id}: lease still held by another harness — leaving it")
                continue

            if dry_run:
                logger.info(f"DRY-RUN: would release stale lock on {issue_id} -> {target}")
                released += 1
                continue

            add, remove = stages.transition(labels, target)
            try:
                for label in add:
                    github.add_label(repo, issue["number"], label)
                for label in remove:
                    github.remove_label(repo, issue["number"], label)
            except GithubClientError as exc:
                logger.warn(f"Could not release stale lock on {issue_id}: {exc}")
                continue

            logger.warn(f"Released stale lock on {issue_id} -> {target}")
            released += 1

    return released


def _lease_is_free(db, issue_id: str, logger: MainLogger) -> bool:
    """True if `issue_id` has no live Postgres lease.

    Fails closed: an unreachable database counts as "cannot verify", which
    this reports as *not* free so the caller leaves the label alone rather
    than guessing - one transient fetch failure should not blind the whole
    sweep the way a `release_expired` failure does, so this degrades
    per-issue instead of aborting the loop.
    """
    try:
        row = db_issue_state.fetch(db, issue_id)
    except DbUnavailable as exc:
        logger.warn(f"Could not verify lease for {issue_id}: {exc}")
        return False
    return row is None or row.get("owner_harness_id") is None


def _warn_stale_lock_hours(config, github, repo: str, logger: MainLogger) -> None:
    """Log if a repo's `staleLockHours` promises a shorter window than the
    real Postgres lease TTL can deliver.

    `staleLockHours` (`pipeline.json`, parsed at `pipeline.py:76,157`) used
    to be auto-claude's only staleness signal and was never read. Now
    `lease_expires_at` (`config.database.lease_ttl_seconds`, Task 11 —
    global to the harness, the same for every repo; `DbSync.acquire_lease`'s
    frozen signature takes no per-repo override) is what actually determines
    staleness. A repo whose `staleLockHours` claims a *shorter* window than
    the harness's configured TTL is promising something the lease system
    cannot keep: a legitimate, still-running worker can look "stuck" to a
    human reading pipeline.json for up to the difference. This changes no
    behaviour - it is a diagnostic only - which is deliberate: it makes the
    field read and acted upon rather than silently discarded, without
    inventing per-repo TTL plumbing the frozen `DbSync` surface does not
    support. Compares against `config.database.lease_ttl_seconds`, not the
    `db.lease.LEASE_TTL_SECONDS` module default, since Task 13 lets an
    operator override that default — this diagnostic must reflect whatever
    is actually running, not the fallback.
    """
    text = _pipeline_json_text(config, github, repo, logger)
    if text is None:
        return
    try:
        pipeline = parse_pipeline_config(text, source=f"{repo}/{PIPELINE_JSON_RELATIVE_PATH}")
    except PipelineConfigError:
        return  # already surfaced elsewhere; not this function's job to repeat it

    ttl_seconds = config.database.lease_ttl_seconds
    configured_seconds = pipeline.stale_lock_hours * 3600
    if configured_seconds < ttl_seconds:
        short_by_minutes = (ttl_seconds - configured_seconds) / 60
        logger.warn(
            f"{repo}: staleLockHours={pipeline.stale_lock_hours}h "
            f"({configured_seconds:.0f}s) is shorter than the harness lease "
            f"TTL ({ttl_seconds}s) — a legitimate in-progress "
            f"run may appear stuck for up to {short_by_minutes:.0f} more minute(s)"
        )
```

Modify `main.py:510` (the call site) — this assumes `db` already exists as a
local variable in `main()` from an earlier phase's wiring (the same `db`
Task 13 assumed to build `dbsync`):

```python
    released = _release_stale_locks(config, github, logger, dry_run=args.dry_run, db=db)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_wiring.py -v`
Expected: PASS (every original `TestReleaseStaleLocks` test unchanged, plus
6 new `TestReleaseStaleLocksWithLease` tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 622 passed, 2 skipped, 0 failed

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_wiring.py
git commit -m "feat(lease): make _release_stale_locks expiry-driven, not startup-driven (ai-cc)"
```
## Phase D — Run and summary capture

### Task 16: `db/history.py`

**Files:**
- Create: `db/history.py`
- Test: `tests/test_db_history.py`

**Interfaces:**
- Consumes: `db.pool.Database.execute(sql: str, params: tuple = ()) -> list[tuple]` (existing, Phase A).
- Produces: `new_id() -> str`, `start_run(db, *, run_id, issue_id, harness_id, mode, model) -> None`, `finish_run(db, *, run_id, outcome, exit_code, duration_seconds, cost_usd, turns, crash_log_path) -> None`, `add_summary(db, *, summary_id, issue_id, run_id, kind, body, comment_url) -> None` — consumed by `dbsync.py` (Task 21).

- [ ] **Step 1: Write the failing test**

```python
"""Tests for db/history.py — run and summary rows must survive journal replay.

Every insert here uses ON CONFLICT (id) DO NOTHING and every update is a
plain last-writer-wins SET. The specific bug this guards: if start_run or
add_summary ever used a naive INSERT, replaying a journaled write a second
time (see db/journal.py) would raise a duplicate-key error and abort the
whole replay batch instead of silently no-op'ing on the row that already
made it to Postgres before the connection dropped.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import history  # noqa: E402


class FakeDatabase:
    """Records every execute() call; never touches a real connection."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return []


class TestNewId:
    def test_returns_a_32_char_hex_string(self):
        value = history.new_id()
        assert len(value) == 32
        assert all(c in "0123456789abcdef" for c in value)

    def test_two_calls_never_collide(self):
        assert history.new_id() != history.new_id()


class TestStartRun:
    def test_inserts_with_on_conflict_do_nothing(self):
        db = FakeDatabase()
        history.start_run(
            db, run_id="run1", issue_id="repo#1", harness_id="h1",
            mode="dev", model="claude-sonnet-4-5",
        )
        assert len(db.calls) == 1
        sql, params = db.calls[0]
        assert "INSERT INTO auto_claude.run" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql
        assert params == ("run1", "repo#1", "h1", "dev", "claude-sonnet-4-5")

    def test_calling_twice_with_the_same_id_is_a_replay_safe_no_op(self):
        # The fake can't enforce the real primary-key constraint, but it
        # proves the function itself never short-circuits or raises on a
        # repeat call — idempotency is delegated entirely to the SQL text
        # asserted above, which is what actually protects a replayed insert.
        db = FakeDatabase()
        kwargs = dict(
            run_id="run1", issue_id="repo#1", harness_id="h1",
            mode="dev", model="claude-sonnet-4-5",
        )
        history.start_run(db, **kwargs)
        history.start_run(db, **kwargs)
        assert len(db.calls) == 2
        assert db.calls[0] == db.calls[1]


class TestFinishRun:
    def test_updates_ended_at_and_outcome(self):
        db = FakeDatabase()
        history.finish_run(
            db, run_id="run1", outcome="completed", exit_code=0,
            duration_seconds=42, cost_usd=1.2345, turns=7,
            crash_log_path=None,
        )
        assert len(db.calls) == 1
        sql, params = db.calls[0]
        assert "UPDATE auto_claude.run" in sql
        assert "ended_at = now()" in sql
        assert params == ("completed", 0, 42, 1.2345, 7, None, "run1")

    def test_calling_twice_is_last_writer_wins_no_op(self):
        db = FakeDatabase()
        history.finish_run(
            db, run_id="run1", outcome="failed", exit_code=1,
            duration_seconds=10, cost_usd=0.5, turns=3,
            crash_log_path="crash_logs/x.log",
        )
        history.finish_run(
            db, run_id="run1", outcome="failed", exit_code=1,
            duration_seconds=10, cost_usd=0.5, turns=3,
            crash_log_path="crash_logs/x.log",
        )
        assert len(db.calls) == 2
        assert db.calls[0] == db.calls[1]


class TestAddSummary:
    def test_inserts_with_on_conflict_do_nothing(self):
        db = FakeDatabase()
        history.add_summary(
            db, summary_id="sum1", issue_id="repo#1", run_id="run1",
            kind="dev", body="Implemented the thing.",
            comment_url="https://github.com/o/r/issues/1#issuecomment-1",
        )
        assert len(db.calls) == 1
        sql, params = db.calls[0]
        assert "INSERT INTO auto_claude.summary" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql
        assert params == (
            "sum1", "repo#1", "run1", "dev", "Implemented the thing.",
            "https://github.com/o/r/issues/1#issuecomment-1",
        )

    def test_run_id_may_be_none_for_runless_summaries(self):
        db = FakeDatabase()
        history.add_summary(
            db, summary_id="sum2", issue_id="repo#1", run_id=None,
            kind="triage", body="Needs more info.", comment_url=None,
        )
        _sql, params = db.calls[0]
        assert params[2] is None

    def test_calling_twice_with_the_same_id_is_a_replay_safe_no_op(self):
        db = FakeDatabase()
        kwargs = dict(
            summary_id="sum1", issue_id="repo#1", run_id="run1",
            kind="dev", body="text", comment_url=None,
        )
        history.add_summary(db, **kwargs)
        history.add_summary(db, **kwargs)
        assert len(db.calls) == 2
        assert db.calls[0] == db.calls[1]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_db_history.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.history'` (or `ImportError: cannot import name 'history'` if `db/__init__.py` already exists from an earlier phase).

- [ ] **Step 3: Write the implementation**

```python
"""auto_claude.run and auto_claude.summary — durable execution history.

Every write here is replay-safe by construction: inserts use
`ON CONFLICT (id) DO NOTHING` and updates are unconditional last-writer-wins
`SET`s, because `db/journal.py` may replay the same call twice if Postgres
drops the connection after committing but before acknowledging.
"""

from __future__ import annotations

import uuid

from db.pool import Database


def new_id() -> str:
    """A replay-safe primary key, generated by the caller, not the database."""
    return uuid.uuid4().hex


def start_run(
    db: Database, *, run_id: str, issue_id: str, harness_id: str,
    mode: str, model: str | None,
) -> None:
    """Open a `run` row. Replay-safe: a second call with the same id is a no-op."""
    db.execute(
        """
        INSERT INTO auto_claude.run (id, issue_id, harness_id, mode, model)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (run_id, issue_id, harness_id, mode, model),
    )


def finish_run(
    db: Database, *, run_id: str, outcome: str, exit_code: int | None,
    duration_seconds: int | None, cost_usd: float | None, turns: int | None,
    crash_log_path: str | None,
) -> None:
    """Close a `run` row. Last-writer-wins: replaying is harmless."""
    db.execute(
        """
        UPDATE auto_claude.run
           SET ended_at = now(), outcome = %s, exit_code = %s,
               duration_seconds = %s, cost_usd = %s, turns = %s,
               crash_log_path = %s
         WHERE id = %s
        """,
        (outcome, exit_code, duration_seconds, cost_usd, turns, crash_log_path, run_id),
    )


def add_summary(
    db: Database, *, summary_id: str, issue_id: str, run_id: str | None,
    kind: str, body: str, comment_url: str | None,
) -> None:
    """Insert a `summary` row. Replay-safe: a second call with the same id is a no-op."""
    db.execute(
        """
        INSERT INTO auto_claude.summary (id, issue_id, run_id, kind, body, comment_url)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (summary_id, issue_id, run_id, kind, body, comment_url),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_db_history.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 631 passed, 2 skipped, 0 failed

- [ ] **Step 6: Commit**

```bash
git add db/history.py tests/test_db_history.py
git commit -m "feat(history): add db/history.py for run and summary rows (ai-cc)"
```

### Task 17: parse cost/turns/duration from the `stream-json` result event

**Files:**
- Modify: `worker.py:1-28` (add `import uuid`)
- Modify: `worker.py:129-141` (`StateUpdate` — no field changes yet, Task 18 extends it)
- Modify: `worker.py:350-483` (`_run_claude` — add `RunMetrics`, `_parse_run_metrics`, `_accumulate_metrics`; change return type to a 5-tuple)
- Modify: `worker.py:770-823` (`_run_handoff_summary` — call site at `worker.py:801`)
- Modify: `worker.py:1584-1868` (`run_dev_worker` — call sites at `worker.py:1651` and `worker.py:1754`)
- Modify: `worker.py:2043-2233` (`run_review_worker` — call site at `worker.py:2121`)
- Modify: `tests/test_review_worker.py:287` (existing `_run_claude` stub returns a 4-tuple; the 5th element is required the moment this task lands or `_stub_happy_path` breaks every test that uses it)
- Test: `tests/test_run_capture.py`

**Interfaces:**
- Consumes: nothing new — pure parsing over the same `stream-json` text `_extract_result_text` already reads.
- Produces: `RunMetrics(cost_usd: float | None, turns: int | None, duration_seconds: int | None)`, `_parse_run_metrics(output: str) -> RunMetrics`, `_accumulate_metrics(base: RunMetrics, extra: RunMetrics) -> RunMetrics`, and `_run_claude(...) -> tuple[int, str, bool, RateLimitInfo | None, RunMetrics]` — consumed by Task 18.

**Why the 4 call sites don't break:** `_run_claude` moves from a 4-tuple to a 5-tuple return. Every one of its 4 existing call sites is updated in this same task, in the same commit, so the signature and its callers change atomically — there is no intermediate state where a 4-tuple unpack meets a 5-tuple return. Two call sites (`worker.py:1754` inside the repair round, `worker.py:801` inside the grace-budget handoff) don't need the metrics for their own logic, so they bind it to a name and either fold it in (`1754`, via `_accumulate_metrics` — a repair round is the same billed task continuing) or discard it (`801` — the handoff summary runs on `ctx.light_model`, a different model paying for a different, smaller job, so mixing its cost into the primary run's single `model` column would misattribute spend).

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_run_capture.py -v`
Expected: FAIL with `ImportError: cannot import name 'RunMetrics' from 'worker'`

- [ ] **Step 3: Write the implementation**

Add near the top of `worker.py` (with the other stdlib imports, `worker.py:1-28`):

```python
import uuid
```

Add just above `_run_claude` (`worker.py:350`), after `_format_review_section` and before the function definition:

```python
@dataclass
class RunMetrics:
    """Cost/turns/duration parsed from a Claude CLI `stream-json` result event.

    All fields are None when no result event was seen — a crash mid-run, or
    output truncated before the CLI got to emit one — so a caller writing
    this to Postgres stores NULL rather than a fabricated zero.
    """
    cost_usd: float | None = None
    turns: int | None = None
    duration_seconds: int | None = None


def _parse_run_metrics(output: str) -> RunMetrics:
    """Extract cost/turns/duration from the LAST `"type":"result"` line.

    Mirrors `_extract_result_text`'s line-by-line parse below. Malformed
    lines are skipped rather than fatal, since stream-json output is
    line-buffered from a subprocess and a truncated final line is routine.
    When more than one result event appears, the last one wins — matching
    the equivalent last-write behaviour a caller would get by re-running
    `_extract_result_text` over the same stream.
    """
    metrics = RunMetrics()
    for line in output.split("\n"):
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("type") != "result":
            continue
        cost = data.get("total_cost_usd")
        turns = data.get("num_turns")
        duration_ms = data.get("duration_ms")
        metrics = RunMetrics(
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            turns=int(turns) if isinstance(turns, int) else None,
            duration_seconds=(
                round(duration_ms / 1000) if isinstance(duration_ms, (int, float)) else None
            ),
        )
    return metrics


def _accumulate_metrics(base: RunMetrics, extra: RunMetrics) -> RunMetrics:
    """Sum two readings from sequential `_run_claude` calls billed to one run.

    Used to fold a same-model repair round into the primary call's totals —
    both bill against the same `run` row. A field stays None only when BOTH
    inputs are None; one usable reading is better than discarding it because
    the other call's result event never arrived.
    """
    def _sum(a: float | int | None, b: float | int | None):
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)

    return RunMetrics(
        cost_usd=_sum(base.cost_usd, extra.cost_usd),
        turns=_sum(base.turns, extra.turns),
        duration_seconds=_sum(base.duration_seconds, extra.duration_seconds),
    )
```

Change `_run_claude`'s signature and both early returns (`worker.py:360-363` and the two `return (-1, "", False, rate_limit)` lines at `worker.py:449` and `worker.py:456`):

```python
def _run_claude(
    prompt: str,
    cwd: Path,
    ctx: IssueContext,
    logger: WorkerLogger,
    abort_event: Event,
    bypass_permissions: bool = True,
    budget_override: float | None = None,
    model_override: str | None = None,
    max_turns_override: int | None = None,
) -> tuple[int, str, bool, RateLimitInfo | None, RunMetrics]:
    """Run Claude CLI via Popen, stream output to logger.

    Returns (returncode, captured_output, budget_exceeded, rate_limit, metrics).

    `rate_limit` is the last limiting rate_limit_event seen, or None. Under
    subscription auth this — not the USD budget — is the constraint that
    actually stops work. `metrics` is parsed from the stream-json result
    event and is all-None if the run crashed before emitting one.
    """
```

```python
            if abort_event.is_set():
                logger.warn("Abort requested — terminating Claude")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return (-1, "", False, rate_limit, RunMetrics())
```

```python
    except Exception as exc:
        logger.error(f"Error reading Claude output: {exc}")
        proc.kill()
        proc.wait()
        return (-1, "", False, rate_limit, RunMetrics())
```

Change the final return (`worker.py:483`):

```python
    captured_output = "\n".join(captured_lines)
    metrics = _parse_run_metrics(captured_output)
    return (proc.returncode, captured_output, budget_exceeded, rate_limit, metrics)
```

Update the 4 call sites. `worker.py:801` (`_run_handoff_summary`, grace-budget call — metrics discarded, different model):

```python
    returncode, output, _, _, _metrics = _run_claude(
        prompt=prompt,
        cwd=cwd,
        ctx=ctx,
        logger=logger,
        abort_event=abort_event,
        bypass_permissions=True,
        budget_override=ctx.grace_budget_usd,
        model_override=ctx.light_model,
        max_turns_override=10,
    )
```

`worker.py:1651` (`run_dev_worker`, primary call — metrics kept):

```python
        returncode, output, budget_exceeded, rate_limit, metrics = _run_claude(
            prompt=prompt,
            cwd=worktree_dir,
            ctx=ctx,
            logger=logger,
            abort_event=abort_event,
            bypass_permissions=True,
        )
```

`worker.py:1754` (`run_dev_worker`, repair round — folded into `metrics` via `_accumulate_metrics`):

```python
            repair_rc, repair_output, _budget, _rl, repair_metrics = _run_claude(
                prompt=_build_repair_prompt(checks_transcript),
                cwd=worktree_dir,
                ctx=ctx,
                logger=logger,
                abort_event=abort_event,
                bypass_permissions=True,
            )
            metrics = _accumulate_metrics(metrics, repair_metrics)
            if repair_rc == 0:
                output += "\n" + repair_output
```

`worker.py:2121` (`run_review_worker`, primary call — metrics kept):

```python
        returncode, output, budget_exceeded, rate_limit, metrics = _run_claude(
            prompt=prompt,
            cwd=worktree_dir,
            ctx=ctx,
            logger=logger,
            abort_event=abort_event,
            bypass_permissions=True,
        )
```

`tests/test_review_worker.py:287` — `_stub_happy_path`'s `_run_claude` stub gains the 5th element:

```python
    monkeypatch.setattr(worker, "_run_claude", lambda **k: (0, verdict, False, None, RunMetrics()))
```

This requires importing `RunMetrics` in that test file's existing `from worker import (...)` block (`tests/test_review_worker.py:25-36`):

```python
from worker import (  # noqa: E402
    IssueContext,
    RunMetrics,
    StateUpdate,
    _candidate_prs_for_issue,
    _extract_review_feedback,
    _labels_for_review_claim,
    _labels_for_review_crash,
    _labels_for_review_fail,
    _labels_for_review_pass,
    _parse_review_verdict,
    run_review_worker,
)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_run_capture.py tests/test_review_worker.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 640 passed, 2 skipped, 0 failed

- [ ] **Step 6: Commit**

```bash
git add worker.py tests/test_run_capture.py tests/test_review_worker.py
git commit -m "feat(history): parse cost/turns/duration from stream-json result events (ai-cc)"
```

### Task 18: emit `run` rows

**Files:**
- Modify: `dbsync.py` (add `start_run`/`finish_run` to the existing `DbSync` class)
- Modify: `worker.py:129-141` (`StateUpdate` — add run identity + metrics fields)
- Modify: `worker.py:1596-1846` (`run_dev_worker` — generate `run_id`, carry it and `metrics` through every exit path)
- Modify: `worker.py:2055-2212` (`run_review_worker` — same)
- Modify: `process_manager.py:1-49` (constructor — optional `dbsync` param, `self._active_runs` map)
- Modify: `process_manager.py:207-275` (`reap_dead` — close a dangling run on an unreported crash)
- Modify: `process_manager.py:277-317` (`drain_state_queue` — open/close `run` rows)
- Modify: `process_manager.py:326-419` (`shutdown_all`, `_mark_interrupted`, `_drain_and_reap_during_shutdown` — close a dangling run on force-terminate)
- Test: `tests/test_dbsync.py` (append), `tests/test_run_capture.py`

**Interfaces:**
- Consumes: `RunMetrics`, `_parse_run_metrics`, `_accumulate_metrics` (Task 17), `db.history.start_run`/`finish_run` (Task 16). Adds `start_run`/`finish_run` to the `DbSync` class Task 8 introduced, following the `_durable` pattern its `upsert_issue` already uses. `process_manager.py` itself stays duck-typed against `self._dbsync` (it still never does `from dbsync import DbSync`) — that is a `process_manager.py` design choice unrelated to whether `dbsync.py` exists, which it has since Task 8.
- Produces: `DbSync.start_run(*, run_id, issue_id, mode, model)` and `DbSync.finish_run(*, run_id, outcome, exit_code, duration_seconds, cost_usd, turns, crash_log_path)` — the frozen `CONTRACT.md` signatures, now real, not just assumed by a duck-typed caller — plus the `run_id`/`run_mode`/`run_model`/`run_outcome`/`exit_code`/`duration_seconds`/`cost_usd`/`turns`/`crash_log_path` fields on `StateUpdate`, and `ProcessManager._active_runs`. Consumed by Task 19 (which adds `summaries` alongside these same messages, and `add_summary` alongside these same `DbSync` methods) and by Task 21 (which upgrades the failure sink these two methods already go through).

- [ ] **Step 0: Add `start_run`/`finish_run` to `DbSync`**

Append to the existing `DbSync` class in `dbsync.py` (Task 13's lease block already
sits above this one; `self._durable` is Task 8's):

```python
    def start_run(self, *, run_id: str, issue_id: str, mode: str, model: str | None) -> None:
        payload = dict(
            run_id=run_id, issue_id=issue_id, harness_id=self._harness.id,
            mode=mode, model=model,
        )
        self._durable("history.start_run", payload, lambda: history.start_run(self._db, **payload))

    def finish_run(self, *, run_id: str, outcome: str, exit_code: int | None,
                   duration_seconds: int | None, cost_usd: float | None, turns: int | None,
                   crash_log_path: str | None) -> None:
        payload = dict(
            run_id=run_id, outcome=outcome, exit_code=exit_code,
            duration_seconds=duration_seconds, cost_usd=cost_usd, turns=turns,
            crash_log_path=crash_log_path,
        )
        self._durable("history.finish_run", payload, lambda: history.finish_run(self._db, **payload))
```

Add `from db import history` to `dbsync.py`'s imports (alongside the existing
`from db import issue_state` — Task 12 already added `from db import lease as
db_lease` there too).

Append to `tests/test_dbsync.py`:

```python
class TestStartAndFinishRun:
    def test_start_run_succeeds_silently_when_postgres_is_reachable(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger())
        sync.start_run(run_id="r1", issue_id="repo#1", mode="dev", model="m")
        assert db.calls

    def test_finish_run_is_logged_and_dropped_on_db_unavailable(self):
        db = FakeDatabase(raises=DbUnavailable("down"))
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger)
        sync.finish_run(run_id="r1", outcome="completed", exit_code=0,
                         duration_seconds=1, cost_usd=0.1, turns=1, crash_log_path=None)
        assert any("dropping" in msg.lower() for _lvl, msg in logger.messages)
```

Run: `.venv\Scripts\python -m pytest tests/test_dbsync.py -v`
Expected: PASS (all `TestEnabled`/`TestUpsertIssueNeverRaises`/lease tests
from Tasks 8 and 13, plus the 2 new ones above)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_run_capture.py` (created in Task 17):

```python


class FakeDbSync:
    """Stand-in for dbsync.DbSync — Task 21 supplies the real one.

    ProcessManager only ever calls methods on this object; it never imports
    the concrete class, so this fake is sufficient to prove the wiring here
    without dbsync.py existing yet.
    """

    def __init__(self):
        self.started: list[dict] = []
        self.finished: list[dict] = []

    def start_run(self, *, run_id, issue_id, mode, model):
        self.started.append(dict(run_id=run_id, issue_id=issue_id, mode=mode, model=model))

    def finish_run(self, *, run_id, outcome, exit_code, duration_seconds,
                    cost_usd, turns, crash_log_path):
        self.finished.append(dict(
            run_id=run_id, outcome=outcome, exit_code=exit_code,
            duration_seconds=duration_seconds, cost_usd=cost_usd, turns=turns,
            crash_log_path=crash_log_path,
        ))


class FakePmLogger:
    def info(self, msg):
        pass

    def warn(self, msg):
        pass

    def error(self, msg):
        pass

    def drain_queue(self, _q):
        pass


class FakePmState:
    def __init__(self, records):
        self._records = records

    def get(self, issue_id):
        return self._records.get(issue_id)

    def transition(self, issue_id, status):
        self._records[issue_id].status = status

    def update(self, issue_id, **kwargs):
        for k, v in kwargs.items():
            setattr(self._records[issue_id], k, v)

    def save(self):
        pass


def _make_pm_with_dbsync(records=None):
    import queue
    from process_manager import ProcessManager

    config = SimpleNamespace(
        workers=SimpleNamespace(max_parallel=3, shutdown_grace_seconds=30, max_continuations=2),
    )
    dbsync = FakeDbSync()
    pm = ProcessManager(
        config=config,
        state=FakePmState(records or {}),
        logger=FakePmLogger(),
        log_queue=queue.Queue(),
        state_queue=queue.Queue(),
        dbsync=dbsync,
    )
    return pm, dbsync


class TestRunRowLifecycleViaDrainStateQueue:
    def test_the_first_update_of_a_run_opens_a_run_row(self):
        rec = SimpleNamespace(status="queued", error=None, branch=None, pr_url=None,
                              worker_pid=None, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._state_queue.put(StateUpdate(
            "r#1", "in_progress", worker_pid=111,
            run_id="run1", run_mode="dev", run_model="claude-sonnet-4-5",
        ))
        pm.drain_state_queue()
        assert dbsync.started == [dict(
            run_id="run1", issue_id="r#1", mode="dev", model="claude-sonnet-4-5",
        )]
        assert pm._active_runs["r#1"] == "run1"

    def test_the_terminal_update_of_a_run_closes_the_run_row(self):
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate(
            "r#1", "completed", run_id="run1", run_outcome="completed",
            exit_code=0, duration_seconds=90, cost_usd=0.42, turns=6,
        ))
        pm.drain_state_queue()
        assert dbsync.finished == [dict(
            run_id="run1", outcome="completed", exit_code=0, duration_seconds=90,
            cost_usd=0.42, turns=6, crash_log_path=None,
        )]
        assert "r#1" not in pm._active_runs

    def test_no_dbsync_wired_is_a_silent_no_op(self):
        # Guards the sequencing gap: main/process_manager wiring may not yet
        # pass a dbsync (Phase C not landed, or Postgres disabled) — this
        # must never raise.
        import queue
        from process_manager import ProcessManager
        rec = SimpleNamespace(status="queued", error=None, branch=None, pr_url=None,
                              worker_pid=None, handoff_summary=None)
        config = SimpleNamespace(
            workers=SimpleNamespace(max_parallel=3, shutdown_grace_seconds=30),
        )
        pm = ProcessManager(
            config=config, state=FakePmState({"r#1": rec}), logger=FakePmLogger(),
            log_queue=queue.Queue(), state_queue=queue.Queue(),
        )
        pm._state_queue.put(StateUpdate(
            "r#1", "in_progress", run_id="run1", run_mode="dev", run_model="m",
        ))
        pm.drain_state_queue()  # must not raise


class TestRunRowClosedOnCrash:
    def test_reap_dead_closes_a_run_left_open_by_a_crash_with_no_update(self):
        rec = SimpleNamespace(status="in_progress", error=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._workers["r#1"] = (FakeProc(exitcode=1), object())

        pm.reap_dead()

        assert dbsync.finished == [dict(
            run_id="run1", outcome="failed", exit_code=1, duration_seconds=None,
            cost_usd=None, turns=None, crash_log_path=None,
        )]
        assert "r#1" not in pm._active_runs

    def test_mark_interrupted_closes_a_run_left_open_by_shutdown(self):
        rec = SimpleNamespace(status="in_progress", error=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"

        pm._mark_interrupted("r#1", exit_code=-15)

        assert dbsync.finished == [dict(
            run_id="run1", outcome="interrupted", exit_code=-15, duration_seconds=None,
            cost_usd=None, turns=None, crash_log_path=None,
        )]
        assert "r#1" not in pm._active_runs

    def test_a_run_already_closed_normally_is_not_double_closed(self):
        # No entry in _active_runs means drain_state_queue already handled it.
        rec = SimpleNamespace(status="completed", error=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._mark_interrupted("r#1", exit_code=0)
        assert dbsync.finished == []


class FakeProc:
    """A worker process that has already exited."""

    def __init__(self, exitcode=1):
        self.exitcode = exitcode
        self.pid = 1234

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_run_capture.py -v`
Expected: FAIL with `TypeError: ProcessManager.__init__() got an unexpected keyword argument 'dbsync'`

- [ ] **Step 3: Write the implementation**

`worker.py:129-141` — extend `StateUpdate`:

```python
@dataclass
class StateUpdate:
    """Status update sent from worker back to main process via state_queue."""
    issue_id: str
    status: str             # IssueStatus value
    error: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    worker_pid: int | None = None
    handoff_summary: str | None = None
    # Epoch seconds until which the supervisor should stop spawning workers.
    # Set when Claude reports a rate limit; see ratelimit.py.
    rate_limited_until: float | None = None
    # Run identity + metrics, threaded from worker back to `main` — the sole
    # Postgres writer — so a `run` row can be opened/closed without the
    # worker touching the database directly. `run_mode`/`run_model` are set
    # on the FIRST update of a run (opens the row); `run_outcome` and the
    # metric fields are set on the LAST update (closes it). A message never
    # carries both halves.
    run_id: str | None = None
    run_mode: str | None = None       # "dev" | "review"
    run_model: str | None = None
    run_outcome: str | None = None    # completed|failed|interrupted|fenced
    exit_code: int | None = None
    duration_seconds: int | None = None
    cost_usd: float | None = None
    turns: int | None = None
    crash_log_path: str | None = None
```

`worker.py:1596-1615` — `run_dev_worker`'s setup, before the `try:`:

```python
    logger = WorkerLogger(log_queue, ctx.issue_id, ctx.color_name, ctx.color_code, ctx.repo)
    pid = os.getpid()

    is_rework = ctx.action == "rework" and ctx.existing_branch
    logger.info(f"Dev worker started (PID {pid}) — action={ctx.action}"
                + (f" [rework #{ctx.rework_count}]" if is_rework else ""))

    # Opened here so it is in scope for every exit path below, including the
    # generic exception handler if a crash happens before Claude ever runs.
    run_id = uuid.uuid4().hex
    metrics = RunMetrics()
    returncode: int | None = None

    # Signal that we're in progress + claim the stage label as a distributed
    # lock. This never touches the kind hint (ac-fix, ac-implement, ...).
    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="in_progress",
        worker_pid=pid,
        run_id=run_id,
        run_mode="dev",
        run_model=ctx.dev_model,
    ))
    _claim_labels(ctx, logger)

    branch = sanitize_branch_name(ctx.title, ctx.number)
    worktree_dir = ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}"
    is_fresh_branch = False  # True if rework fell back to a new versioned branch
```

`worker.py:1667-1687` — rate-limit exit:

```python
        if rate_limit is not None:
            logger.warn("Rate limited — pushing partial work and re-queueing")
            partial_pr = _push_partial_work(ctx, branch, worktree_dir, logger)

            _run_cmd(
                ["git", "worktree", "remove", str(worktree_dir), "--force"],
                cwd=repo_dir, logger=logger,
            )
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)
            _run_cmd(["git", "worktree", "prune"], cwd=repo_dir, logger=logger)

            state_queue.put(StateUpdate(
                issue_id=ctx.issue_id,
                status="failed",
                error="rate_limited",
                branch=branch,
                pr_url=partial_pr or ctx.pr_url,
                rate_limited_until=_rate_limited_until(rate_limit),
                run_id=run_id,
                run_outcome="failed",
                exit_code=returncode,
                duration_seconds=metrics.duration_seconds,
                cost_usd=metrics.cost_usd,
                turns=metrics.turns,
            ))
            return
```

`worker.py:1690-1714` — budget exit:

```python
        if budget_exceeded:
            logger.warn("Budget exceeded — running handoff summary")
            handoff = _run_handoff_summary(ctx, worktree_dir, output, logger, abort_event)

            # Push any partial work so the next agent can pick it up
            partial_pr = _push_partial_work(ctx, branch, worktree_dir, logger)

            # Cleanup worktree
            _run_cmd(
                ["git", "worktree", "remove", str(worktree_dir), "--force"],
                cwd=repo_dir, logger=logger,
            )
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)
            _run_cmd(["git", "worktree", "prune"], cwd=repo_dir, logger=logger)

            state_queue.put(StateUpdate(
                issue_id=ctx.issue_id,
                status="failed",
                error="budget_exceeded",
                branch=branch,
                pr_url=partial_pr or ctx.pr_url,
                handoff_summary=handoff,
                run_id=run_id,
                run_outcome="failed",
                exit_code=returncode,
                duration_seconds=metrics.duration_seconds,
                cost_usd=metrics.cost_usd,
                turns=metrics.turns,
            ))
            return
```

`worker.py:1826-1831` — success exit (inside `run_dev_worker`, after `_post_issue_report`):

```python
        # Success
        logger.info(f"Completed successfully — PR: {pr_url}")
        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="completed",
            branch=branch,
            pr_url=pr_url,
            run_id=run_id,
            run_outcome="completed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
        ))
```

`worker.py:1833-1846` — the generic exception handler:

```python
    except Exception as exc:
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Worker failed: {exc}")

        # Write crash log and post comment
        log_path = _write_crash_log(ctx, error_detail, logger)
        _post_crash_comment(ctx, str(exc), log_path, logger)

        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error=str(exc),
            run_id=run_id,
            run_outcome="failed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            crash_log_path=str(log_path) if log_path else None,
        ))
```

`worker.py:2055-2063` — `run_review_worker`'s setup:

```python
    logger = WorkerLogger(log_queue, ctx.issue_id, ctx.color_name, ctx.color_code, ctx.repo)
    pid = os.getpid()
    logger.info(f"Review worker started (PID {pid}) — PR {ctx.pr_url}")

    run_id = uuid.uuid4().hex
    metrics = RunMetrics()
    returncode: int | None = None

    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="in_progress",
        worker_pid=pid,
        run_id=run_id,
        run_mode="review",
        run_model=ctx.dev_model,
    ))
```

`worker.py:2170-2174` — review pass exit:

```python
            state_queue.put(StateUpdate(
                issue_id=ctx.issue_id,
                status="completed",
                pr_url=ctx.pr_url,
                run_id=run_id,
                run_outcome="completed",
                exit_code=returncode,
                duration_seconds=metrics.duration_seconds,
                cost_usd=metrics.cost_usd,
                turns=metrics.turns,
            ))
            return
```

`worker.py:2193-2198` — review fail exit:

```python
        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error="blocked" if blocked else "review_fail",
            pr_url=ctx.pr_url,
            run_id=run_id,
            run_outcome="failed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
        ))
```

`worker.py:2200-2212` — review worker's generic exception handler:

```python
    except Exception as exc:
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Review worker failed: {exc}")

        log_path = _write_crash_log(ctx, error_detail, logger)
        _post_crash_comment(ctx, str(exc), log_path, logger)

        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error=str(exc),
            run_id=run_id,
            run_outcome="failed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            crash_log_path=str(log_path) if log_path else None,
        ))
```

`process_manager.py` — constructor gains ONE new attribute.

> **Do not re-declare `__init__`.** Task 13 already added `dbsync` and
> `harness_id` parameters and the `self._dbsync` / `self._harness_id`
> assignments. Rewriting the constructor here would silently delete Task 13's
> lease wiring and break `_lease_ok`. Add the single attribute below
> immediately after the existing `self._harness_id = harness_id` line, and
> change nothing else in the signature or the body.

```python
        # ---- add directly below `self._harness_id = harness_id` ----
        # issue_id -> run_id for the run row currently open on that issue's
        # active worker. Populated by the worker's first StateUpdate,
        # cleared by its terminal one. Anything still here when a worker is
        # reaped means the terminal update never arrived — see
        # `_close_dangling_run`.
        self._active_runs: dict[str, str] = {}
```

After this task the constructor signature is, in full:

```python
    def __init__(
        self,
        config: Config,
        state: StateStore,
        logger: MainLogger,
        log_queue: Queue,
        state_queue: Queue,
        dbsync: DbSync | None = None,
        harness_id: str | None = None,
    ) -> None:
```

`process_manager.py:207-233` — `reap_dead`'s crash branch:

```python
    def reap_dead(self) -> None:
        """Check for dead workers, handle retries."""
        dead: list[str] = []

        for issue_id, (proc, _abort_event) in self._workers.items():
            if not proc.is_alive():
                dead.append(issue_id)

        for issue_id in dead:
            proc, _abort_event = self._workers.pop(issue_id)
            self._color_assigner.release(issue_id)
            proc.join(timeout=5)

            record = self._state.get(issue_id)
            if record is None:
                continue

            exitcode = proc.exitcode
            self._logger.info(
                f"Worker for {issue_id} exited (code={exitcode}, status={record.status})"
            )

            # If the worker crashed without sending a status update, mark failed
            if record.status == IssueStatus.IN_PROGRESS:
                self._state.transition(issue_id, IssueStatus.FAILED)
                self._state.update(issue_id, error=f"Worker crashed (exit code {exitcode})")
                self._state.save()
                record = self._state.get(issue_id)
                self._close_dangling_run(issue_id, outcome="failed", exit_code=exitcode)
```

The rest of `reap_dead` (the rate-limited / budget-exceeded / plain-failed branches below this) is unchanged.

`process_manager.py:277-317` — `drain_state_queue`:

```python
    def drain_state_queue(self) -> None:
        """Process all pending StateUpdate messages from workers."""
        while True:
            try:
                update: StateUpdate = self._state_queue.get_nowait()
            except Exception:
                break

            # Rate limiting is account-wide, so honour it before anything that
            # can `continue` — an unknown issue or a rejected transition must
            # not cause the pool-wide pause to be dropped on the floor.
            self._note_rate_limit(update)

            # Run rows are opened/closed independently of whether the issue
            # transition below succeeds — a worker that raced main's own
            # bookkeeping (e.g. the issue was already terminal) still ran and
            # still needs its cost/duration captured.
            self._sync_run(update)

            record = self._state.get(update.issue_id)
            if record is None:
                continue

            try:
                self._state.transition(update.issue_id, update.status)
            except Exception as exc:
                self._logger.warn(
                    f"State transition failed for {update.issue_id}: {exc}"
                )
                continue

            # Apply optional fields
            updates = {}
            if update.error is not None:
                updates["error"] = update.error
            if update.branch is not None:
                updates["branch"] = update.branch
            if update.pr_url is not None:
                updates["pr_url"] = update.pr_url
            if update.worker_pid is not None:
                updates["worker_pid"] = update.worker_pid
            if update.handoff_summary is not None:
                updates["handoff_summary"] = update.handoff_summary
            if updates:
                self._state.update(update.issue_id, **updates)

            self._state.save()

    def _sync_run(self, update: StateUpdate) -> None:
        """Open or close a `run` row from a worker's StateUpdate. No-op if
        no dbsync is wired (see __init__)."""
        if self._dbsync is None:
            return

        if update.run_mode is not None:
            self._active_runs[update.issue_id] = update.run_id
            self._dbsync.start_run(
                run_id=update.run_id, issue_id=update.issue_id,
                mode=update.run_mode, model=update.run_model,
            )

        if update.run_outcome is not None:
            run_id = self._active_runs.pop(update.issue_id, update.run_id)
            self._dbsync.finish_run(
                run_id=run_id, outcome=update.run_outcome,
                exit_code=update.exit_code, duration_seconds=update.duration_seconds,
                cost_usd=update.cost_usd, turns=update.turns,
                crash_log_path=update.crash_log_path,
            )
```

`process_manager.py:326-419` — `shutdown_all`, `_mark_interrupted`, `_drain_and_reap_during_shutdown`:

```python
    def shutdown_all(self, grace_seconds: int | None = None) -> None:
        """Gracefully shut down all workers."""
        if not self._workers:
            return

        if grace_seconds is None:
            grace_seconds = self._config.workers.shutdown_grace_seconds

        self._logger.info(f"Shutting down {len(self._workers)} worker(s)...")

        # Set abort on all workers
        for issue_id, (_proc, abort_event) in self._workers.items():
            abort_event.set()

        # Wait for graceful exit
        deadline = time.monotonic() + grace_seconds
        while self._workers and time.monotonic() < deadline:
            self._drain_and_reap_during_shutdown()
            time.sleep(0.5)

        # Force-terminate any remaining
        for issue_id, (proc, _abort_event) in list(self._workers.items()):
            if proc.is_alive():
                self._logger.warn(f"Force-terminating worker for {issue_id}")
                proc.terminate()
                proc.join(timeout=5)

            self._mark_interrupted(issue_id, exit_code=proc.exitcode)

        self._workers.clear()

        # Final drain
        self.drain_state_queue()
        self._logger.drain_queue(self._log_queue)
```

```python
    def _mark_interrupted(self, issue_id: str, exit_code: int | None = None) -> None:
        """Leave an aborted worker's issue in a status the poller can resurrect.

        `poller` only re-queues a known issue from FAILED/COMPLETED/INTERRUPTED,
        so a record left at IN_PROGRESS is stranded for good: relabelling it
        ac-dev-ready does nothing, and `_release_stale_locks` only rewinds
        GitHub labels, never the state store. Guarded on IN_PROGRESS so a worker
        that reported a terminal status before the abort landed keeps it —
        callers must drain the state queue first, which both do.
        """
        record = self._state.get(issue_id)
        if record and record.status == IssueStatus.IN_PROGRESS:
            self._state.transition(issue_id, IssueStatus.INTERRUPTED)
            self._state.save()
            self._close_dangling_run(issue_id, outcome="interrupted", exit_code=exit_code)

    def _close_dangling_run(self, issue_id: str, *, outcome: str, exit_code: int | None) -> None:
        """Close a `run` row the worker itself never got to close.

        Reached when a worker process dies without sending a terminal
        StateUpdate — a hard crash (outcome="failed", from `reap_dead`) or a
        force-terminate during shutdown (outcome="interrupted", from
        `_mark_interrupted`). `_active_runs` is populated from the worker's
        first StateUpdate and popped by its terminal one; if this issue_id is
        still in the map, that terminal message never arrived, so none of
        the metric fields are known — they go in as NULL.
        """
        if self._dbsync is None:
            return
        run_id = self._active_runs.pop(issue_id, None)
        if run_id is None:
            return
        self._dbsync.finish_run(
            run_id=run_id, outcome=outcome, exit_code=exit_code,
            duration_seconds=None, cost_usd=None, turns=None, crash_log_path=None,
        )

    def _drain_and_reap_during_shutdown(self) -> None:
        """Drain queues and remove dead workers during shutdown."""
        self.drain_state_queue()
        self._logger.drain_queue(self._log_queue)

        dead = [
            issue_id
            for issue_id, (proc, _) in self._workers.items()
            if not proc.is_alive()
        ]
        for issue_id in dead:
            proc, _ = self._workers.pop(issue_id)
            self._color_assigner.release(issue_id)
            proc.join(timeout=5)
            # A worker that obeys abort and exits inside the grace period is
            # reaped here, which pops it before shutdown_all's force-terminate
            # loop can see it. Without this the "mark interrupted" pass there
            # ran over an empty dict and every clean Ctrl+C stranded its issue.
            self._mark_interrupted(issue_id, exit_code=proc.exitcode)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_run_capture.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 648 passed, 2 skipped, 0 failed

- [ ] **Step 6: Commit**

```bash
git add worker.py process_manager.py tests/test_run_capture.py
git commit -m "feat(history): open and close auto_claude.run rows around worker runs (ai-cc)"
```

### Task 19: emit `summary` rows

**Files:**
- Modify: `dbsync.py` (add `add_summary` to the existing `DbSync` class)
- Modify: `worker.py:129-141` (`StateUpdate` — add `summaries`)
- Modify: `worker.py:177-206` (`_post_crash_comment` — return `(comment_url, body)`)
- Modify: `worker.py:665-711` (`_post_issue_report` — return `(body, comment_url)`)
- Modify: `worker.py:2022-2040` (`_post_pr_review` — return `comment_url`)
- Modify: `worker.py:1596-1846` (`run_dev_worker` — collect `pending_summaries`, post them on every exit)
- Modify: `worker.py:2055-2212` (`run_review_worker` — same)
- Modify: `tests/test_review_worker.py:293,358` (existing `_post_crash_comment` stubs return `None`; must return a 2-tuple now)
- Modify: `github_client.py:164-172` (`post_comment` — return the created comment's URL)
- Modify: `process_manager.py:277-321` (`_sync_run` — post `summaries`, write a `kind="fenced"` summary when `update.error` starts with `"fenced:"`; new `dbsync` property)
- Modify: `process_manager.py:421-465` (`_post_budget_comment` — capture the URL, add a `budget` summary)
- Modify: `main.py:234-296` (`_run_triage` — accept `dbsync`, add a `triage` summary)
- Modify: `main.py:368,548-549,555-556` (`_run_triage` call sites — pass `process_manager.dbsync`)
- Test: `tests/test_dbsync.py` (append), `tests/test_run_capture.py`

**Interfaces:**
- Consumes: `StateUpdate.summaries` slot from Task 18, `db.history.add_summary` (Task 16). Adds `add_summary` to the `DbSync` class Task 8 introduced (alongside Task 18's `start_run`/`finish_run`), following the same `_durable` pattern. `process_manager.py` calls `self._dbsync.add_summary(*, issue_id, run_id, kind, body, comment_url)` — the frozen `DbSync` signature, still duck-typed (no `from dbsync import DbSync` in `process_manager.py`), now against a real implementation instead of an assumed one.
- Produces: five call sites (`dev`, `review`, `crash`, `budget`, `triage`) that now know the exact comment they posted and its URL, plus a sixth, non-comment path — `_sync_run` recognising `update.error.startswith("fenced:")` and writing a `kind="fenced"` summary itself, since a fenced worker (Task 14) never posts a comment to report through. `DbSync.add_summary` (real as of this task) is what everything above writes through.

**Note on `gh` stdout:** `gh issue comment` prints the created comment's URL as its only stdout line on success — verified against the `cli/cli` source (`Comment` and `PullRequestReview` API structs both carry a `url` field; `gh pr create` is confirmed via `fmt.Fprintln(opts.IO.Out, pr.URL)` to follow the same "print the URL" convention as every other create-ish command). `gh pr review` does **not** print anything on success, so `_post_pr_review` below fetches the URL with a follow-up `gh api .../reviews --jq '.[-1].html_url'` call instead of trusting stdout. Every capture in this task is defensive regardless (`startswith("http")` or the call is treated as unresolved): a missing URL degrades `comment_url` to `NULL`, never to a wrong URL, and never fails the post itself.

- [ ] **Step 0: Add `add_summary` to `DbSync`**

Append to the existing `DbSync` class in `dbsync.py`:

```python
    def add_summary(self, *, issue_id: str, run_id: str | None, kind: str,
                    body: str, comment_url: str | None = None) -> str:
        # Generated up front, not inside _durable: the caller (and the
        # log line, if this ends up dropped) must get back the same id that
        # will eventually land in Postgres, whether that happens now or once
        # Task 21's journal replay applies it.
        summary_id = history.new_id()
        payload = dict(
            summary_id=summary_id, issue_id=issue_id, run_id=run_id,
            kind=kind, body=body, comment_url=comment_url,
        )
        self._durable(
            "history.add_summary", payload, lambda: history.add_summary(self._db, **payload)
        )
        return summary_id
```

Append to `tests/test_dbsync.py`:

```python
class TestAddSummary:
    def test_returns_a_stable_id_even_when_the_write_is_dropped(self):
        db = FakeDatabase(raises=DbUnavailable("down"))
        sync = DbSync(db, HARNESS, FakeLogger())
        summary_id = sync.add_summary(issue_id="repo#1", run_id=None, kind="triage", body="text")
        assert isinstance(summary_id, str) and len(summary_id) == 32

    def test_succeeds_silently_when_postgres_is_reachable(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger())
        sync.add_summary(issue_id="repo#1", run_id="r1", kind="dev", body="did it")
        assert db.calls
```

Run: `.venv\Scripts\python -m pytest tests/test_dbsync.py -v`
Expected: PASS (every prior `DbSync` test, plus the 2 new ones above)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_run_capture.py`:

```python


class TestPostIssueReportReturnsBodyAndUrl:
    def test_returns_the_exact_body_and_the_comment_url_on_success(self, tmp_path, monkeypatch):
        import worker

        ctx = SimpleNamespace(
            number=7, org="o", repo="r", dev_model="m",
        )
        monkeypatch.setattr(worker, "_get_issue_labels", lambda ctx, logger: [])
        monkeypatch.setattr(
            worker, "_run_cmd",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="https://github.com/o/r/issues/7#issuecomment-1\n",
                stderr="",
            ),
        )
        body, url = worker._post_issue_report(
            ctx, output="", summary="did the thing", branch="ac/issue-7-x",
            pr_url="https://github.com/o/r/pull/7", outcome="success",
            logger=SimpleNamespace(warn=lambda *a: None),
        )
        assert "did the thing" in body
        assert url == "https://github.com/o/r/issues/7#issuecomment-1"

    def test_returns_none_url_when_the_post_fails(self, tmp_path, monkeypatch):
        import worker

        ctx = SimpleNamespace(number=7, org="o", repo="r", dev_model="m")
        monkeypatch.setattr(worker, "_get_issue_labels", lambda ctx, logger: [])
        monkeypatch.setattr(
            worker, "_run_cmd",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        )
        body, url = worker._post_issue_report(
            ctx, output="", summary="s", branch="b", pr_url=None,
            outcome="success", logger=SimpleNamespace(warn=lambda *a: None),
        )
        assert url is None
        assert "s" in body or body is not None


class TestPostCrashCommentReturnsUrlAndBody:
    def test_returns_the_url_and_the_exact_posted_body(self, tmp_path, monkeypatch):
        import worker

        ctx = SimpleNamespace(number=3, org="o", repo="r")

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(
                returncode=0, stdout="https://github.com/o/r/issues/3#issuecomment-9\n",
                stderr="",
            )

        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        url, body = worker._post_crash_comment(
            ctx, "boom", None, SimpleNamespace(error=lambda *a: None),
        )
        assert url == "https://github.com/o/r/issues/3#issuecomment-9"
        assert "boom" in body


class TestPostPrReviewReturnsUrlFromApiLookup:
    def test_looks_up_the_review_url_after_a_successful_post(self, monkeypatch):
        import worker

        ctx = SimpleNamespace(pr_url="https://github.com/o/r/pull/4", org="o", repo="r")
        calls = []

        def fake_run_cmd(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "review"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(
                returncode=0, stdout="https://github.com/o/r/pull/4#pullrequestreview-1\n",
                stderr="",
            )

        monkeypatch.setattr(worker, "_run_cmd", fake_run_cmd)
        url = worker._post_pr_review(
            ctx, SimpleNamespace(warn=lambda *a: None), approve=True, body="looks good",
        )
        assert url == "https://github.com/o/r/pull/4#pullrequestreview-1"
        assert any(c[:2] == ["gh", "api"] for c in calls)

    def test_no_pr_number_returns_none_without_calling_gh(self, monkeypatch):
        import worker

        ctx = SimpleNamespace(pr_url=None, org="o", repo="r")
        monkeypatch.setattr(
            worker, "_run_cmd", lambda *a, **k: pytest.fail("must not call gh with no PR")
        )
        url = worker._post_pr_review(
            ctx, SimpleNamespace(warn=lambda *a: None), approve=True, body="x",
        )
        assert url is None


class TestProcessManagerPostsSummaries:
    def test_drain_state_queue_writes_every_summary_on_the_terminal_update(self):
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate(
            "r#1", "completed", run_id="run1", run_outcome="completed",
            summaries=[
                {"kind": "dev", "body": "did it", "comment_url": "https://x/1"},
            ],
        ))
        pm.drain_state_queue()
        assert getattr(dbsync, "summaries", None) == [
            dict(issue_id="r#1", run_id="run1", kind="dev", body="did it",
                 comment_url="https://x/1"),
        ]

    def test_no_summaries_field_posts_nothing(self):
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate("r#1", "completed", run_id="run1", run_outcome="completed"))
        pm.drain_state_queue()
        assert getattr(dbsync, "summaries", []) == []

    def test_dbsync_property_exposes_the_wired_instance(self):
        pm, dbsync = _make_pm_with_dbsync({})
        assert pm.dbsync is dbsync

    def test_a_fenced_error_writes_a_fenced_summary_row_even_with_no_summaries_list(self):
        # Task 14's _handle_lease_lost sends error="fenced: ..." with
        # summaries=None (it never touches GitHub, so there is no comment to
        # report) — that hand-off must still produce a Postgres summary row,
        # or it silently falls through the gap.
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate(
            "r#1", "failed", error="fenced: lease lost", run_id="run1",
            run_outcome="failed",
        ))
        pm.drain_state_queue()
        assert getattr(dbsync, "summaries", None) == [
            dict(issue_id="r#1", run_id="run1", kind="fenced",
                 body="fenced: lease lost", comment_url=None),
        ]

    def test_a_non_fenced_error_does_not_write_a_fenced_summary(self):
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate(
            "r#1", "failed", error="budget_exceeded", run_id="run1", run_outcome="failed",
        ))
        pm.drain_state_queue()
        assert getattr(dbsync, "summaries", []) == []
```

`FakeDbSync` (Task 18) needs an `add_summary` method for the two tests above — extend it in place:

```python
class FakeDbSync:
    """Stand-in for dbsync.DbSync — Task 21 supplies the real one."""

    def __init__(self):
        self.started: list[dict] = []
        self.finished: list[dict] = []
        self.summaries: list[dict] = []

    def start_run(self, *, run_id, issue_id, mode, model):
        self.started.append(dict(run_id=run_id, issue_id=issue_id, mode=mode, model=model))

    def finish_run(self, *, run_id, outcome, exit_code, duration_seconds,
                    cost_usd, turns, crash_log_path):
        self.finished.append(dict(
            run_id=run_id, outcome=outcome, exit_code=exit_code,
            duration_seconds=duration_seconds, cost_usd=cost_usd, turns=turns,
            crash_log_path=crash_log_path,
        ))

    def add_summary(self, *, issue_id, run_id, kind, body, comment_url):
        self.summaries.append(dict(
            issue_id=issue_id, run_id=run_id, kind=kind, body=body, comment_url=comment_url,
        ))
```

(This replaces the Task 18 `FakeDbSync` definition in `tests/test_run_capture.py` — same class, `add_summary` added.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_run_capture.py -v`
Expected: FAIL — `_post_issue_report`/`_post_crash_comment` still return `None`/single values, so `body, url = worker._post_issue_report(...)` raises `TypeError: cannot unpack non-iterable NoneType object` (or similar) on the first new test.

- [ ] **Step 3: Write the implementation**

`worker.py:129-141` — add one field to `StateUpdate` (after `crash_log_path`):

```python
    crash_log_path: str | None = None
    # Comments this run posted to GitHub, to be written as `summary` rows
    # alongside the closing update. A list because some failure paths post
    # more than one comment for the same terminal event (e.g. a
    # failed-checks report from `_post_issue_report`, followed by the crash
    # comment from the outer exception handler) — each is its own row. Each
    # dict has keys {"kind", "body", "comment_url"}.
    summaries: list[dict] | None = None
```

`worker.py:177-206` — `_post_crash_comment`:

```python
def _post_crash_comment(
    ctx: IssueContext,
    error: str,
    log_path: Path | None,
    logger: WorkerLogger,
) -> tuple[str | None, str]:
    """Post a concise failure comment on the issue referencing the local crash log.

    Returns (comment_url, body) — the exact text posted and the URL of the
    created comment (None if the post failed), for the caller's `summary`
    row.
    """
    log_ref = f"\n\nCrash log: `{log_path}`" if log_path else ""
    body = redact(
        f"**auto-claude** failed while processing this issue.\n\n"
        f"> {error[:200]}{log_ref}\n\n"
        f"_Re-label to retry after investigating._"
    )
    env = build_env(current_token())
    try:
        result = subprocess.run(
            [
                "gh", "issue", "comment", str(ctx.number),
                "--repo", f"{ctx.org}/{ctx.repo}",
                "--body", body,
            ],
            text=True,
            capture_output=True,
            timeout=30,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return None, body
        url = result.stdout.strip()
        return (url if url.startswith("http") else None), body
    except Exception as exc:
        logger.error(f"Failed to post crash comment: {exc}")
        return None, body
```

`worker.py:665-711` — `_post_issue_report`:

```python
def _post_issue_report(
    ctx: IssueContext,
    *,
    output: str,
    summary: str,
    branch: str,
    pr_url: str | None,
    outcome: str,
    logger: WorkerLogger,
    notes_override: str | None = None,
) -> tuple[str, str | None]:
    """Post the agent's plan, summary and notes back to the issue.

    Never fatal. A worker that wrote the code, pushed it and opened a PR has
    done its job; failing it over a comment would send a completed issue back
    round the retry loop and produce a second PR.

    Returns (body, comment_url) — the exact text posted and the URL of the
    created comment (None if the post failed or the URL could not be
    determined), for the caller's `summary` row.
    """
    body = ""
    try:
        try:
            attempt = stages.attempt_of(_get_issue_labels(ctx, logger))
        except Exception:
            attempt = None

        body = _issue_report(
            plan=_extract_block(output, "IMPLEMENTATION_PLAN"),
            summary=summary,
            notes=notes_override or _extract_block(output, "IMPLEMENTATION_NOTES"),
            attempt=attempt,
            model=ctx.dev_model,
            branch=branch,
            pr_url=pr_url,
            outcome=outcome,
        )
        result = _run_cmd(
            [
                "gh", "issue", "comment", str(ctx.number),
                "--repo", f"{ctx.org}/{ctx.repo}",
                "--body", body,
            ],
            logger=logger,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warn(f"Could not post issue report: {result.stderr.strip()}")
            return body, None
        url = result.stdout.strip()
        return body, (url if url.startswith("http") else None)
    except Exception as exc:
        logger.warn(f"Could not post issue report: {exc}")
        return body, None
```

`worker.py:2022-2040` — `_post_pr_review`:

```python
def _post_pr_review(
    ctx: IssueContext,
    logger: WorkerLogger,
    *,
    approve: bool,
    body: str,
) -> str | None:
    """Post an approving or changes-requested review on the PR via gh.

    Returns the URL of the created review, or None if there was no PR number
    to review, the review post failed, or the follow-up lookup below did.
    `gh pr review` prints nothing on success, so the URL is fetched with a
    second call against the REST reviews endpoint rather than guessed at.
    """
    pr_number = _pr_number(ctx.pr_url)
    if pr_number is None:
        logger.warn("No PR number to review — skipping gh pr review")
        return None
    args = [
        "gh", "pr", "review", str(pr_number),
        "--repo", f"{ctx.org}/{ctx.repo}",
        "--approve" if approve else "--request-changes",
        "--body", redact(body),
    ]
    result = _run_cmd(args, logger=logger, timeout=30)
    if result.returncode != 0:
        logger.warn(f"Could not post PR review: {result.stderr.strip()}")
        return None

    lookup = _run_cmd(
        [
            "gh", "api",
            f"repos/{ctx.org}/{ctx.repo}/pulls/{pr_number}/reviews",
            "--jq", ".[-1].html_url",
        ],
        logger=logger,
        timeout=30,
    )
    url = lookup.stdout.strip()
    return url if lookup.returncode == 0 and url.startswith("http") else None
```

`worker.py:1596-1615` — `run_dev_worker` setup gains `pending_summaries`:

```python
    logger = WorkerLogger(log_queue, ctx.issue_id, ctx.color_name, ctx.color_code, ctx.repo)
    pid = os.getpid()

    is_rework = ctx.action == "rework" and ctx.existing_branch
    logger.info(f"Dev worker started (PID {pid}) — action={ctx.action}"
                + (f" [rework #{ctx.rework_count}]" if is_rework else ""))

    # Opened here so it is in scope for every exit path below, including the
    # generic exception handler if a crash happens before Claude ever runs.
    run_id = uuid.uuid4().hex
    metrics = RunMetrics()
    returncode: int | None = None
    pending_summaries: list[dict] = []

    # Signal that we're in progress + claim the stage label as a distributed
    # lock. This never touches the kind hint (ac-fix, ac-implement, ...).
    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="in_progress",
        worker_pid=pid,
        run_id=run_id,
        run_mode="dev",
        run_model=ctx.dev_model,
    ))
    _claim_labels(ctx, logger)

    branch = sanitize_branch_name(ctx.title, ctx.number)
    worktree_dir = ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}"
    is_fresh_branch = False  # True if rework fell back to a new versioned branch
```

`worker.py:1766-1783` — the checks-still-failing branch:

```python
        if not checks_ok:
            # Nothing broken gets pushed. The transcript goes to the issue so
            # the next attempt starts from the actual failure.
            logger.error("Checks still failing after repair — refusing to push")
            report_body, report_url = _post_issue_report(
                ctx,
                output=output,
                summary="",
                branch=branch,
                pr_url=None,
                outcome="failed",
                logger=logger,
                notes_override=(
                    "Verify/test commands failed and a repair round did not fix "
                    f"them. Nothing was pushed.\n\n```\n{checks_transcript[-3000:]}\n```"
                ),
            )
            pending_summaries.append(
                {"kind": "dev", "body": report_body, "comment_url": report_url}
            )
            raise RuntimeError("Verify/test checks failed — not pushing")
```

`worker.py:1810-1831` — the success path:

```python
        report_body, report_url = _post_issue_report(
            ctx,
            output=output,
            summary=summary,
            branch=branch,
            pr_url=pr_url,
            outcome="success",
            logger=logger,
        )
        pending_summaries.append(
            {"kind": "dev", "body": report_body, "comment_url": report_url}
        )

        # Success
        logger.info(f"Completed successfully — PR: {pr_url}")
        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="completed",
            branch=branch,
            pr_url=pr_url,
            run_id=run_id,
            run_outcome="completed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            summaries=pending_summaries or None,
        ))
```

`worker.py:1833-1846` — the generic exception handler:

```python
    except Exception as exc:
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Worker failed: {exc}")

        # Write crash log and post comment
        log_path = _write_crash_log(ctx, error_detail, logger)
        crash_url, crash_body = _post_crash_comment(ctx, str(exc), log_path, logger)
        pending_summaries.append({"kind": "crash", "body": crash_body, "comment_url": crash_url})

        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error=str(exc),
            run_id=run_id,
            run_outcome="failed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            crash_log_path=str(log_path) if log_path else None,
            summaries=pending_summaries or None,
        ))
```

`worker.py:2055-2063` — `run_review_worker` setup gains `pending_summaries`:

```python
    logger = WorkerLogger(log_queue, ctx.issue_id, ctx.color_name, ctx.color_code, ctx.repo)
    pid = os.getpid()
    logger.info(f"Review worker started (PID {pid}) — PR {ctx.pr_url}")

    run_id = uuid.uuid4().hex
    metrics = RunMetrics()
    returncode: int | None = None
    pending_summaries: list[dict] = []

    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="in_progress",
        worker_pid=pid,
        run_id=run_id,
        run_mode="review",
        run_model=ctx.dev_model,
    ))
```

`worker.py:2161-2175` — the review-pass path:

```python
        if review_pass:
            logger.info("Review pass — approving and handing off to ac-hitl")
            approve_body = (
                "Agent review: build passes, diff satisfies acceptance criteria, "
                "security pass clean. Handing to human for final test (HITL gate)."
            )
            review_url = _post_pr_review(ctx, logger, approve=True, body=approve_body)
            pending_summaries.append(
                {"kind": "review", "body": approve_body, "comment_url": review_url}
            )
            _review_pass_labels(ctx, logger)

            state_queue.put(StateUpdate(
                issue_id=ctx.issue_id,
                status="completed",
                pr_url=ctx.pr_url,
                run_id=run_id,
                run_outcome="completed",
                exit_code=returncode,
                duration_seconds=metrics.duration_seconds,
                cost_usd=metrics.cost_usd,
                turns=metrics.turns,
                summaries=pending_summaries or None,
            ))
            return
```

`worker.py:2177-2198` — the review-fail path:

```python
        feedback = _extract_review_feedback(result_text)
        if not checks_ok:
            feedback = f"Verify/test checks failed:\n{checks_transcript}\n\n{feedback}"

        blocked = _review_fail_labels(ctx, logger)
        if blocked:
            logger.warn("Attempts exhausted — issue moved to ac-blocked")
            request_body = (
                "Agent review: circuit breaker — this issue has failed review 3 "
                f"or more times and cannot converge automatically.\n\n{feedback}"
            )
        else:
            logger.info("Review fail — requesting changes and returning to ac-dev-ready")
            request_body = f"Agent review: changes needed.\n\n{feedback}"
        review_url = _post_pr_review(ctx, logger, approve=False, body=request_body)
        pending_summaries.append(
            {"kind": "review", "body": request_body, "comment_url": review_url}
        )

        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error="blocked" if blocked else "review_fail",
            pr_url=ctx.pr_url,
            run_id=run_id,
            run_outcome="failed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            summaries=pending_summaries or None,
        ))
```

`worker.py:2200-2212` — the review worker's exception handler:

```python
    except Exception as exc:
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Review worker failed: {exc}")

        log_path = _write_crash_log(ctx, error_detail, logger)
        crash_url, crash_body = _post_crash_comment(ctx, str(exc), log_path, logger)
        pending_summaries.append({"kind": "crash", "body": crash_body, "comment_url": crash_url})

        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error=str(exc),
            run_id=run_id,
            run_outcome="failed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            crash_log_path=str(log_path) if log_path else None,
            summaries=pending_summaries or None,
        ))
```

`tests/test_review_worker.py:293` and `:358` — the existing `_post_crash_comment` stubs must return a 2-tuple now the caller unpacks it:

```python
    monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: (None, "crash body"))
```

(applies to both occurrences — line 293 inside `_stub_happy_path`, line 358 inside `test_no_pr_found_does_not_approve_or_crash_and_releases_the_lock`)

`github_client.py:164-172` — `post_comment` returns the created comment's URL:

```python
    def post_comment(self, repo: str, number: int, body: str) -> str | None:
        """Post a comment on an issue. Returns the created comment's URL.

        `gh issue comment` prints the new comment's URL as its only stdout
        line on success. Returns None if that line is missing or does not
        look like a URL — callers must treat the result as best-effort, not
        as proof the comment exists (`_run_gh` already raised on a hard
        failure, so this only covers an unparseable success).
        """
        result = self._run_gh(
            [
                "issue", "comment", str(number),
                "--repo", f"{self.org}/{repo}",
                "--body", body,
            ]
        )
        url = result.stdout.strip()
        return url if url.startswith("http") else None
```

`process_manager.py:277-321` — extend `_sync_run` to post summaries, and add a public `dbsync` property:

```python
    def _sync_run(self, update: StateUpdate) -> None:
        """Open/close a `run` row and post any `summary` rows from a worker's
        StateUpdate. No-op if no dbsync is wired (see __init__).

        A fenced worker (Task 14's `_handle_lease_lost`) is read-only — it
        cannot write to Postgres itself, only `main` can — so it hands off
        by sending `error="fenced: ..."` on the terminal StateUpdate and
        nothing in `summaries` (posting a GitHub comment is exactly the
        remote touch fencing exists to prevent, so there is no comment_url
        to carry). That hand-off is completed HERE: an `error` starting
        with "fenced:" gets its own `kind="fenced"` summary row, written to
        Postgres (not GitHub) via `self._dbsync`, which is not the remote
        act the fence guards against — only `main` ever calls this, from the
        already-fenced-safe write seam.
        """
        if self._dbsync is None:
            return

        if update.run_mode is not None:
            self._active_runs[update.issue_id] = update.run_id
            self._dbsync.start_run(
                run_id=update.run_id, issue_id=update.issue_id,
                mode=update.run_mode, model=update.run_model,
            )

        if update.run_outcome is not None:
            run_id = self._active_runs.pop(update.issue_id, update.run_id)
            self._dbsync.finish_run(
                run_id=run_id, outcome=update.run_outcome,
                exit_code=update.exit_code, duration_seconds=update.duration_seconds,
                cost_usd=update.cost_usd, turns=update.turns,
                crash_log_path=update.crash_log_path,
            )

        if update.error and update.error.startswith("fenced:"):
            self._dbsync.add_summary(
                issue_id=update.issue_id, run_id=update.run_id, kind="fenced",
                body=update.error, comment_url=None,
            )

        for item in (update.summaries or []):
            self._dbsync.add_summary(
                issue_id=update.issue_id, run_id=update.run_id,
                kind=item["kind"], body=item["body"],
                comment_url=item.get("comment_url"),
            )

    @property
    def dbsync(self):
        """The wired DbSync instance (may be None) — read by `main` so
        runless summaries (triage, budget) can be posted through the same
        seam without ProcessManager owning that logic itself."""
        return self._dbsync
```

`process_manager.py:421-465` — `_post_budget_comment` captures the URL and posts a `budget` summary:

```python
    def _post_budget_comment(self, record: IssueRecord) -> None:
        """Post a comment when budget was exceeded across max continuation runs."""
        try:
            import subprocess
            env = build_env(current_token())
            max_cont = self._config.workers.max_continuations
            budget = self._config.claude.max_budget_usd
            total = budget * (max_cont + 1)
            body = redact(
                f"**auto-claude** exceeded its budget across {record.continuation_count} "
                f"continuation run(s) (${budget}/run, ~${total:.2f} total).\n\n"
                f"The issue may be too large for automated handling at the current budget. "
                f"Partial work has been pushed to branch `{record.branch}`.\n\n"
                f"_Consider breaking this into smaller issues, or increase "
                f"`max_budget_usd` in config._"
            )
            result = subprocess.run(
                [
                    "gh", "issue", "comment", str(record.number),
                    "--repo", f"{self._config.github.org}/{record.repo}",
                    "--body", body,
                ],
                text=True,
                capture_output=True,
                timeout=30,
                env=env,
                encoding="utf-8",
                errors="replace",
            )
            comment_url = result.stdout.strip()
            if result.returncode != 0 or not comment_url.startswith("http"):
                comment_url = None

            # Runless — the decision to give up is made here, across
            # continuation runs that each already closed their own `run` row.
            if self._dbsync is not None:
                self._dbsync.add_summary(
                    issue_id=record.issue_id, run_id=None, kind="budget",
                    body=body, comment_url=comment_url,
                )
        except Exception as exc:
            self._logger.error(f"Failed to post budget comment on {record.issue_id}: {exc}")
```

`main.py:234-296` — `_run_triage` accepts `dbsync` and posts a `triage` summary:

```python
def _run_triage(record, state, github, triage_engine, config, logger,
                dry_run: bool = False, dbsync=None) -> None:
    """Triage a single issue and update state accordingly."""
    # Triage answers "is this issue specified well enough to implement". That
    # question is meaningless for a PR review, and a needs-info verdict would
    # bounce the issue to ac-input-needed and strand an open PR. Queue directly.
    if record.mode == "review":
        # The poller usually lands these on QUEUED itself; --issue mode does
        # not. Transitioning QUEUED -> QUEUED is not a legal move, so only
        # move a record that has not arrived yet.
        if record.status != IssueStatus.QUEUED:
            state.transition(record.issue_id, IssueStatus.QUEUED)
            state.save()
        logger.info(f"{record.issue_id} -> queued for review (triage skipped)")
        return

    logger.info(f"Triaging {record.issue_id}...")
    state.transition(record.issue_id, IssueStatus.TRIAGING)
    state.update(record.issue_id, triage_attempts=record.triage_attempts + 1)
    state.save()

    decision = triage_engine.triage(record)
    logger.info(
        f"Triage {decision.decision.upper()} ({decision.confidence}) — {decision.summary}"
    )

    if decision.decision == "proceed":
        state.transition(record.issue_id, IssueStatus.QUEUED)
        state.save()
        logger.info(f"{record.issue_id} -> {state.get(record.issue_id).status}")
    else:
        state.transition(record.issue_id, IssueStatus.NEEDS_INFO)
        state.save()

        if not dry_run:
            comment = format_clarifying_comment(decision, config)
            try:
                comment_url = github.post_comment(record.repo, record.number, comment)
                # Move the stage backwards off ac-dev-ready: the issue is not
                # ready after all, and leaving the trigger label on would make
                # the next poll pick it straight back up.
                add, remove = stages.transition(record.labels, "ac-input-needed")
                for label in add:
                    github.add_label(record.repo, record.number, label)
                for label in remove:
                    github.remove_label(record.repo, record.number, label)
                # Re-fetch updated_at so the poller doesn't treat our own
                # comment as a user response and immediately re-triage
                try:
                    fresh = github.get_issue(record.repo, record.number)
                    state.update(record.issue_id,
                                 issue_updated_at=fresh.get("updated_at", ""))
                    state.save()
                except Exception:
                    pass
                # Runless — triage is an inline call from `main`, not a
                # worker run, so `run_id` is NULL by design.
                if dbsync is not None:
                    dbsync.add_summary(
                        issue_id=record.issue_id, run_id=None, kind="triage",
                        body=comment, comment_url=comment_url,
                    )
                logger.info(f"Posted clarifying questions on {record.issue_id}")
            except Exception as exc:
                logger.error(f"Failed to post comment on {record.issue_id}: {exc}")
        else:
            logger.info(f"DRY-RUN: would post clarifying questions on {record.issue_id}")
```

`main.py:368` — inside `_run_single_issue`:

```python
    if record.status in (IssueStatus.DISCOVERED,):
        _run_triage(record, state, github, triage_engine, config, logger,
                    dbsync=process_manager.dbsync)
        record = state.get(issue_id)
```

`main.py:548-549` and `main.py:555-556` — inside the main polling loop:

```python
                _run_triage(record, state, github, triage_engine, config, logger,
                            dry_run=args.dry_run, dbsync=process_manager.dbsync)
```

(applies to both the new-issues loop at `548-549` and the retriage loop at `555-556`)

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_run_capture.py tests/test_review_worker.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 660 passed, 2 skipped, 0 failed

- [ ] **Step 6: Commit**

```bash
git add worker.py process_manager.py main.py github_client.py tests/test_run_capture.py tests/test_review_worker.py
git commit -m "feat(history): capture comment URLs and post auto_claude.summary rows (ai-cc)"
```

## Phase E — Journal and replay

### Task 20: `db/journal.py`

**Files:**
- Create: `db/journal.py`
- Test: `tests/test_db_journal.py`

**Interfaces:**
- Consumes: `db.pool.Database`, `db.pool.DbUnavailable` (Phase A); `db.issue_state.upsert`, `db.history.start_run`, `db.history.finish_run`, `db.history.add_summary` (Task 16 + Phase B); `db.harness.Harness`, `db.harness.register` (Phase A).
- Produces: `Journal(path: Path)`, `.append(op, payload) -> None`, `.pending() -> int`, `.replay(db) -> int` — the frozen signature from `CONTRACT.md`, consumed by `dbsync.py` (Task 21).

- [ ] **Step 1: Write the failing test**

```python
"""Tests for db/journal.py — the append-only fallback when Postgres is down.

Every op the journal can hold is idempotent by construction (Task 16's
ON CONFLICT DO NOTHING / last-writer-wins), so replaying the same file twice
must be silently harmless. The specific failure this guards against: if
`replay()` ever truncated the journal file *before* confirming every entry
applied, a connection drop on entry 3 of 5 would discard entries 4 and 5
along with the ones that already succeeded — silent data loss with no way to
recover the lost writes, since the only other copy was Postgres itself,
which is exactly what was unreachable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.journal import Journal  # noqa: E402
from db.pool import DbUnavailable  # noqa: E402


class FakeDatabase:
    """Records every execute() call; fails on the Nth call if `fail_at` is set."""

    def __init__(self, fail_at: int | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self._fail_at = fail_at

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if self._fail_at is not None and len(self.calls) == self._fail_at:
            raise DbUnavailable("connection dropped mid-replay")
        return []


class TestJournalAppendAndReplay:
    def test_replay_applies_every_entry_in_order(self, tmp_path):
        db = FakeDatabase()
        journal = Journal(tmp_path / "journal.jsonl")
        journal.append("history.start_run", dict(
            run_id="run1", issue_id="repo#1", harness_id="h1", mode="dev", model="m",
        ))
        journal.append("history.finish_run", dict(
            run_id="run1", outcome="completed", exit_code=0, duration_seconds=10,
            cost_usd=0.1, turns=2, crash_log_path=None,
        ))

        applied = journal.replay(db)

        assert applied == 2
        assert len(db.calls) == 2
        assert "INSERT INTO auto_claude.run" in db.calls[0][0]
        assert "UPDATE auto_claude.run" in db.calls[1][0]
        assert journal.pending() == 0

    def test_replaying_twice_is_a_no_op(self, tmp_path):
        db = FakeDatabase()
        journal = Journal(tmp_path / "journal.jsonl")
        journal.append(
            "harness.register", dict(id="h1", hostname="box", pid=1, version="0.2.0")
        )

        first = journal.replay(db)
        second = journal.replay(db)

        assert first == 1
        assert second == 0
        assert len(db.calls) == 1, "the second replay must not re-apply anything"

    def test_a_db_failure_mid_replay_leaves_the_journal_intact_and_untruncated(self, tmp_path):
        db = FakeDatabase(fail_at=2)
        journal = Journal(tmp_path / "journal.jsonl")
        for i in range(3):
            journal.append("history.start_run", dict(
                run_id=f"run{i}", issue_id="repo#1", harness_id="h1",
                mode="dev", model="m",
            ))

        with pytest.raises(DbUnavailable):
            journal.replay(db)

        assert journal.pending() == 3, "nothing may be lost when the DB drops mid-replay"

    def test_file_is_created_lazily(self, tmp_path):
        path = tmp_path / "nested" / "journal.jsonl"
        journal = Journal(path)
        assert not path.exists()
        journal.append(
            "harness.register", dict(id="h1", hostname="box", pid=1, version="0.2.0")
        )
        assert path.exists()

    def test_pending_is_zero_before_any_append(self, tmp_path):
        journal = Journal(tmp_path / "journal.jsonl")
        assert journal.pending() == 0

    def test_each_line_is_valid_jsonl(self, tmp_path):
        journal = Journal(tmp_path / "journal.jsonl")
        journal.append("issue_state.upsert", dict(issue_id="repo#1"))
        journal.append("issue_state.upsert", dict(issue_id="repo#2"))

        lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        for line in lines:
            entry = json.loads(line)
            assert set(entry.keys()) == {"op", "payload"}

    def test_unknown_op_is_rejected_on_append(self, tmp_path):
        journal = Journal(tmp_path / "journal.jsonl")
        with pytest.raises(ValueError):
            journal.append("not.a.real.op", {})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_db_journal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.journal'`

- [ ] **Step 3: Write the implementation**

```python
"""db/journal.py — the append-only fallback for writes made while Postgres
was unreachable.

One JSON object per line: {"op": <str>, "payload": <dict>}. Every op maps to
an idempotent call from db/history.py, db/issue_state.py or db/harness.py
(ON CONFLICT DO NOTHING inserts, last-writer-wins updates), because replay
may legitimately run the same entry twice — see `replay()`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from db import harness as db_harness
from db import history as db_history
from db import issue_state as db_issue_state
from db.pool import Database

_OP_HANDLERS: dict[str, Callable[[Database, dict], None]] = {
    "issue_state.upsert": lambda db, payload: db_issue_state.upsert(db, **payload),
    "history.start_run": lambda db, payload: db_history.start_run(db, **payload),
    "history.finish_run": lambda db, payload: db_history.finish_run(db, **payload),
    "history.add_summary": lambda db, payload: db_history.add_summary(db, **payload),
    "harness.register": lambda db, payload: db_harness.register(
        db, db_harness.Harness(**payload)
    ),
}


class Journal:
    """Append-only JSONL of writes made while Postgres was unreachable.

    One JSON object per line: {"op": <str>, "payload": <dict>}.
    Every op must be idempotent, because replay may run twice.
    """

    OPS = (
        "issue_state.upsert", "history.start_run",
        "history.finish_run", "history.add_summary", "harness.register",
    )

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
        with self._path.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def replay(self, db: Database) -> int:
        """Apply every entry in order, then truncate. Returns entries applied.

        Raises DbUnavailable without truncating if the DB drops mid-replay —
        the file keeps every entry, including the ones already applied
        before the drop, so the next successful replay re-runs them too.
        That is safe only because every handler above is idempotent; this is
        the one place in the codebase allowed to rely on that.
        """
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        if not lines:
            return 0

        applied = 0
        for line in lines:
            entry = json.loads(line)
            op = entry["op"]
            payload = entry["payload"]
            handler = _OP_HANDLERS.get(op)
            if handler is None:
                raise ValueError(f"Unknown journal op: {op!r}")
            handler(db, payload)  # DbUnavailable propagates uncaught — see docstring
            applied += 1

        # Every entry applied without the DB dropping — safe to truncate.
        self._path.write_text("", encoding="utf-8")
        return applied
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_db_journal.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 667 passed, 2 skipped, 0 failed

- [ ] **Step 6: Commit**

```bash
git add db/journal.py tests/test_db_journal.py
git commit -m "feat(history): add db/journal.py for durable-write replay (ai-cc)"
```

### Task 21: `dbsync.py` — the journal upgrade

**Files:**
- Modify: `dbsync.py` (`_durable` journals instead of logging-and-dropping; add `replay_pending`)
- Modify: `main.py` (`_init_db_layer` constructs a real `Journal` and threads it into `DbSync`; `replay_pending()` called alongside the heartbeat)
- Test: `tests/test_dbsync.py` (append), `tests/test_startup_db_wiring.py` (append)

**Interfaces:**
- Consumes: `db.journal.Journal` (Task 20); the `DbSync` class Task 8 introduced and Tasks 13/18/19 grew.
- Produces: `DbSync._durable` now journals on `DbUnavailable` instead of logging and dropping — no signature changed to make this possible, `journal` has been an accepted (if previously unused) `__init__` argument since Task 8; `DbSync.replay_pending() -> int` (frozen in `CONTRACT.md`); `main._init_db_layer` now constructs a real `Journal` and returns `(db, journal, harness, dbsync)` — a 4-tuple again, `journal` reinstated after Task 11 dropped it for lack of anything to construct.

This is deliberately the smallest task in the plan: everything durable writes
and lease operations do was already built and tested (Tasks 8, 13, 18, 19).
The only things that did not exist until now are the `Journal` class itself
(Task 20) and somewhere in `main` willing to construct one.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dbsync.py`. Every existing `DbSync(...)` construction in
that file already passes `journal=` as a keyword (Task 8's `*` splits
`journal`/`ttl_seconds` off as keyword-only) — none of it needs to change for
this task; only the *behaviour* of a failed durable write changes, from
log-and-drop to journal-and-replay.

```python
class TestDurableWritesNowJournalInsteadOfBeingDropped:
    """The Task 8 -> Task 21 upgrade: every durable write already routed
    through `_durable`; only its failure branch changes here, from a log
    line to `self._journal.append(op, payload)`. Nothing above `_durable`
    (upsert_issue, start_run, finish_run, add_summary) changes at all."""

    def test_upsert_issue_journals_and_does_not_raise_on_db_unavailable(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise

        assert journal.pending() == 1

    def test_start_run_journals_on_db_unavailable(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        sync.start_run(run_id="r1", issue_id="repo#1", mode="dev", model="m")

        assert journal.pending() == 1

    def test_finish_run_journals_on_db_unavailable(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        sync.finish_run(
            run_id="r1", outcome="completed", exit_code=0, duration_seconds=1,
            cost_usd=0.1, turns=1, crash_log_path=None,
        )

        assert journal.pending() == 1

    def test_add_summary_journals_and_still_returns_a_stable_id(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        summary_id = sync.add_summary(
            issue_id="repo#1", run_id=None, kind="triage", body="text",
        )

        assert isinstance(summary_id, str) and len(summary_id) == 32
        assert journal.pending() == 1

    def test_a_non_connectivity_error_is_still_logged_and_dropped_not_journaled(self, tmp_path):
        # A bad payload (e.g. a body too long for the column) must not
        # journal forever against a write that will never succeed — this
        # branch of _durable is untouched by the upgrade.
        db = FakeDatabase(raises=ValueError("value too long"))
        journal = Journal(tmp_path / "j.jsonl")
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger, journal=journal)

        sync.start_run(run_id="r1", issue_id="repo#1", mode="dev", model="m")

        assert journal.pending() == 0
        assert any("failed" in msg.lower() for _lvl, msg in logger.messages)

    def test_no_db_at_all_journals_directly(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(None, HARNESS, FakeLogger(), journal=journal)

        sync.start_run(run_id="r1", issue_id="repo#1", mode="dev", model="m")

        assert journal.pending() == 1


class TestLeaseOperationsStillNeverJournal:
    """Regression guard for the upgrade above: a real Journal is now wired
    in, so it would be easy to accidentally route a lease operation through
    it. `acquire_lease`/`check_lease` must keep failing closed and queuing
    nothing — see the comment on that section of DbSync. "Claims never
    queue" (spec, 12-shared-state-in-postgres.md): a journaled claim would
    silently replay later and double-claim an issue another harness has
    since taken over, which is the exact bug the lease exists to prevent."""

    def test_acquire_lease_returns_false_and_does_not_journal_on_db_unavailable(
        self, tmp_path, monkeypatch
    ):
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(FakeDatabase(), HARNESS, FakeLogger(), journal=journal)
        monkeypatch.setattr(
            dbsync.lease, "acquire",
            lambda *a, **k: (_ for _ in ()).throw(DbUnavailable("down")),
        )

        result = sync.acquire_lease("repo#1")

        assert result is False
        assert journal.pending() == 0, "claims must fail closed, never queue"

    def test_check_lease_delegates_to_lease_check_and_never_journals(
        self, tmp_path, monkeypatch
    ):
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(FakeDatabase(), HARNESS, FakeLogger(), journal=journal)
        monkeypatch.setattr(dbsync.lease, "check", lambda *a, **k: False)

        assert sync.check_lease("repo#1") is False
        assert journal.pending() == 0


class TestReplayPending:
    def test_drains_the_journal_once_the_db_is_back(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        journal.append(
            "history.start_run",
            dict(run_id="r1", issue_id="repo#1", harness_id="h1", mode="dev", model="m"),
        )
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        applied = sync.replay_pending()

        assert applied == 1
        assert journal.pending() == 0

    def test_returns_zero_with_no_db(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        journal.append(
            "history.start_run",
            dict(run_id="r1", issue_id="repo#1", harness_id="h1", mode="dev", model="m"),
        )
        sync = DbSync(None, HARNESS, FakeLogger(), journal=journal)

        assert sync.replay_pending() == 0
        assert journal.pending() == 1
```

Add `from db.journal import Journal` and `import dbsync` (for the
`monkeypatch.setattr(dbsync.lease, ...)` calls above) to `tests/test_dbsync.py`'s
imports if not already present from an earlier task's additions.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_dbsync.py -v`
Expected: FAIL — the new `TestDurableWritesNowJournalInsteadOfBeingDropped`
tests fail because `_durable` still logs-and-drops (Task 8's behaviour);
`journal.pending()` is `0` where the tests expect `1`.

- [ ] **Step 3: Upgrade `_durable`, add `replay_pending`**

Replace `dbsync.py`'s `_durable` method (Task 8) in place:

```python
    def _durable(self, op: str, payload: dict, call) -> None:
        """Run a durable write. Journal it on DbUnavailable; log and drop it
        on any other error, since journaling a write that will never succeed
        (a bad payload, a constraint violation) would retry it forever
        against the same broken data every time the journal replays.

        Upgraded here from Task 8's log-and-drop sink to a real journal —
        `self._journal` is a `db.journal.Journal` now (main.py constructs
        one below), not `None`. Nothing about `_durable`'s callers
        (upsert_issue, start_run, finish_run, add_summary) or DbSync's
        constructor signature changed to make this possible; `journal` has
        been an accepted argument since Task 8.
        """
        if self._db is None:
            self._journal.append(op, payload)
            return
        try:
            call()
        except DbUnavailable as exc:
            self._logger.warn(f"Postgres unreachable — journaling {op}: {exc}")
            self._journal.append(op, payload)
        except Exception as exc:
            self._logger.error(f"Durable write {op} failed (not journaled): {exc}")
```

Append a new section to the `DbSync` class, after the lease-operations block
(Task 13):

```python
    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def replay_pending(self) -> int:
        if self._db is None:
            return 0
        try:
            return self._journal.replay(self._db)
        except DbUnavailable as exc:
            self._logger.warn(f"Replay stopped — Postgres unreachable again: {exc}")
            return 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_dbsync.py -v`
Expected: PASS (every `DbSync` test from Tasks 8, 13, 18, 19, 21)

- [ ] **Step 5: Construct a real `Journal` in `main` and wire in replay**

Add `from db.journal import Journal` to `main.py`'s module-level imports,
alongside `from dbsync import DbSync` (Task 11) — `db/journal.py` now exists
(Task 20), so this is a normal import, not a lazy one.

Also upgrade `_register_harness` (Task 11) to journal instead of log-and-drop
— `db/journal.py` lists `"harness.register"` as a real op (its
`_OP_HANDLERS` already implements it), so this closes the last log-and-drop
sink left over from before `db/journal.py` existed:

```python
def _register_harness(db, harness, logger: MainLogger, journal) -> None:
    """Best-effort harness registration. A registration that cannot reach
    Postgres is journaled and replayed the next time Postgres is reachable —
    see Journal.OPS, which lists "harness.register" as a legitimate op."""
    if db is None:
        return
    try:
        db_harness.register(db, harness)
    except DbUnavailable as exc:
        journal.append("harness.register", {
            "id": harness.id, "hostname": harness.hostname,
            "pid": harness.pid, "version": harness.version,
        })
        logger.warn(f"Postgres unreachable while registering the harness — journaled: {exc}")
```

Modify `main.py`'s `_init_db_layer` (Task 11) to construct a real `Journal`
and pass it through:

```python
def _init_db_layer(config, logger: MainLogger):
    """Construct the Postgres-backed layer main() writes through, or a fully
    degraded stand-in when it is disabled/unreachable. Returns
    (db, journal, harness, dbsync) — db is None in degraded mode; journal,
    harness and dbsync always exist so callers never branch on "did this
    construct".
    """
    journal = Journal(config.database.journal_file)
    harness = new_harness(version.__version__)

    db = None
    url = config.database.url() if config.database.enabled else None
    if url:
        db = Database(url, connect_timeout=config.database.connect_timeout_seconds)
        _check_schema_gate(db, logger)
        _register_harness(db, harness, logger, journal)
    else:
        logger.info(
            "Database sync disabled or PIPELINE_METRICS_DATABASE_URL unset — "
            "running on local state only"
        )

    dbsync = DbSync(db, harness, logger, journal=journal,
                     ttl_seconds=config.database.lease_ttl_seconds)
    return db, journal, harness, dbsync
```

Update the "Initialize core components" block (Task 11) for the 4-tuple:

```python
    db, journal, harness, dbsync = _init_db_layer(config, logger)
```

Modify the poll loop (Task 13's two `_maybe_heartbeat` call sites) to also
attempt a replay right alongside each heartbeat — cheap to call even when
the journal is empty or the database is still down (`replay_pending` is a
no-op in both cases):

```python
            last_heartbeat = _maybe_heartbeat(
                dbsync, last_heartbeat, config.database.heartbeat_interval_seconds, logger
            )
            dbsync.replay_pending()
```

(The trailing `logger` argument is not a typo and is not optional: Task 13's
fix round added it as a required 4th positional parameter so `_maybe_heartbeat`
can warn when Postgres is unreachable instead of letting `DbUnavailable` kill
the poll loop. Match the live signature in `main.py`, not any earlier draft of
this document.)

(applies to both the top-of-loop call site and the per-second sleep-loop call
site Task 13 added.)

- [ ] **Step 6: Write the wiring test**

Append to `tests/test_startup_db_wiring.py`:

```python
class TestInitDbLayerConstructsARealJournal:
    def test_init_db_layer_returns_a_four_tuple_with_a_real_journal(self, tmp_path):
        cfg = _config(_db_config(enabled=False))
        cfg.database.journal_file = tmp_path / "journal.jsonl"
        db, journal, _harness, dbsync = main._init_db_layer(cfg, _logger())
        assert db is None
        assert isinstance(journal, main.Journal)
        assert dbsync._journal is journal

    def test_register_harness_now_journals_instead_of_only_logging(self, tmp_path, monkeypatch):
        # Task 11 could only log-and-drop a failed registration — db/journal.py
        # did not exist yet. Now that it does, a registration that cannot
        # reach Postgres must be queued for replay, not merely logged.
        harness = main.new_harness("0.2.0")
        journal = main.Journal(tmp_path / "journal.jsonl")
        monkeypatch.setattr(
            main.db_harness, "register",
            lambda *a, **k: (_ for _ in ()).throw(main.DbUnavailable("down")),
        )
        main._register_harness(object(), harness, _logger(), journal)
        assert journal.pending() == 1
```

- [ ] **Step 7: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: 679 passed, 2 skipped, 0 failed

- [ ] **Step 8: Commit**

```bash
git add dbsync.py main.py tests/test_dbsync.py tests/test_startup_db_wiring.py
git commit -m "feat(history): journal durable writes on failure and replay them once Postgres returns (ai-cc)"
```

---

## Appendix: user rulings made during implementation

Six questions escalated to the user while executing this plan. Each was a
point where the plan contradicted itself or the brief, so the answer is not
derivable from the code alone — but the *consequence* of each is now in the
codebase, at the sites listed below. Recorded here because the scratch ledger
they were logged in (`.superpowers/sdd/13-shared-state-implementation/`) was
deliberately deleted after the feature shipped.

| # | Date | Ruling | Where it lives now |
|---|---|---|---|
| 1 | 2026-07-29 | **Refuse to start** when Postgres is unreachable — a harness that cannot take a lease could double-claim an issue another box is working. Startup and runtime are deliberately opposite: startup refuses on unreachable/stale schema, runtime never aborts a running agent. | `main.py` `_check_schema_gate` docstring; the table in "Degraded operation" above; `tests/test_startup_db_wiring.py` |
| 2 | 2026-07-30 | Runtime `DbUnavailable` **must be caught, not propagated** — the poll loop's only handler is `except KeyboardInterrupt`, so an escape kills the supervisor with live workers attached. `_lease_ok` warns and returns False (fail closed); `_maybe_heartbeat` warns but still advances `last_at` so a down database cannot hot-loop; `reap_dead`'s release warns and keeps reaping. `DbUnavailable` only — never bare `Exception`. | `process_manager.py` `_lease_ok` / `reap_dead`; `main.py` `_maybe_heartbeat` |
| 3 | 2026-07-30 | `_release_stale_locks` **keeps warn-and-continue** on an unreachable database, despite `_check_schema_gate` aborting on the same condition. It runs after the gate, so it is only reachable in the window where Postgres dies between the two — and ruling 2 means no worker spawns anyway. | `main.py` `_release_stale_locks` docstring |
| 4 | 2026-07-30 | Structurally bad journal lines are **quarantined**, not dropped: appended verbatim to a sibling `.corrupt` file, logged once at error, skipped. A transient `DbUnavailable` must still stop replay and leave the journal intact — the two must not collapse into one handler. | `db/journal.py` `replay()` |
| 5 | 2026-07-30 | With **no database configured at all**, durable writes are **logged and dropped**, not journaled. GitHub labels stay truth and startup reconciliation rebuilds `issues.json` every restart. | `dbsync.py` `_durable`; `tests/test_dbsync.py` |
| 6 | 2026-07-30 | `replay_pending()` runs **only at the top of the poll loop**, never on the per-second sleep tick — with `Database(retries=2, connect_timeout=10)` a failing execute burns ~33s of backoff and defeats responsive shutdown. | `main.py`, single call site in the poll loop |
