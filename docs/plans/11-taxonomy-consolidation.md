# 11 — ac-* Taxonomy Consolidation

Consolidate auto-claude's label vocabulary with the `accelevation-claude-tools`
loop pipeline so the two systems share one state machine and never contend for
the same issue.

**Decision:** the loop's stage machine is canonical. auto-claude adopts it.

**Status:** proposed — not yet implemented.

---

## 1. Why

Both systems drive `ac-*` labels on the same GitHub org, and both currently
target `Accelevation/field_admin` off base branch `derekdev`:

- auto-claude — `config.toml` lists field_admin in `[github].repos`
- the loop — `field_admin/.claude/pipeline.json` exists and is configured

They disagree on what labels *mean*:

| | auto-claude | loop |
|---|---|---|
| Model | Imperative verbs (`ac-implement` = "do this") | Stage machine (label *is* the state) |
| Source of truth | `state/issues.json` | The labels themselves |
| Human gates | none | 3 (`ac-dev-ready`, `ac-hitl`, `ac-merged`) |
| Runtime | Standalone Python daemon, multi-repo, N parallel | In-session `/loop`, one repo per `pipeline.json` |

### The concrete failure

`ac-in-progress` and `ac-pr-created` are shared names with different owners.

1. auto-claude picks up `ac-implement`, sets `ac-in-progress`.
2. The loop's `loop-pipeline-planner` reads that as a dev-agent self-lock it owns.
3. After `staleLockHours: 2` it resets the issue to `ac-dev-ready`, comments
   "stale lock detected", and dispatches `loop-dev-agent`.
4. Two agents now work one issue → two branches, two PRs into `derekdev`.

Not currently colliding, and worth preserving:

- Branch names — auto-claude `ac/issue-<n>-<slug>` (`worker.py:138`) vs loop `issue-<n>`
- Worktrees — auto-claude `worktrees/<repo>/issue-<n>` vs loop `.claude/worktrees/slot-<n>`
- Label matching is exact-set (`poller.py:167`), so loop stage labels do not
  spuriously trigger auto-claude workers today.

---

## 2. Target model

Two orthogonal axes. The systems were conflating them.

**Stage axis** — canonical, exactly one per issue, owned by whichever runner owns the repo:

```
ac-pending-review → ac-dev-ready → ac-in-progress → ac-dev-review
                                 → ac-review-in-progress → ac-hitl → ac-merged → ac-done
```

Plus `ac-input-needed` (needs human), `ac-blocked` (failed 3×, human-only).

**Control:** `ac-reviewed`, `ac-attempt-1|2|3`, `ac-pr-created`.

**Kind axis** — auto-claude's verbs, demoted. They no longer trigger anything;
they inform prompt selection and model routing only.

### Mapping

| auto-claude today | Becomes |
|---|---|
| `ac-needs-info` | `ac-input-needed` |
| `ac-in-progress` | `ac-in-progress` (unchanged — already aligned) |
| `ac-pr-created` | `ac-pr-created` (unchanged — already aligned) |
| `ac-plan-posted`, `ac-review-posted` | `ac-reviewed` |
| `ac-fix`, `ac-implement`, `ac-test`, `ac-rework` | kind hints; stop triggering |
| `ac-plan`, `ac-review` | **retired** — see below |
| *(new trigger)* | `ac-dev-ready` |

### Retiring the plan/review worker

Decided 2026-07-27: auto-claude's plan/review path is retired outright.
`loop-triage-agent` subsumes it — both produced a spec-gap comment, and keeping
two was the main source of vocabulary drift (`ac-plan-posted` /
`ac-review-posted` exist only because of it).

Removes: `run_plan_worker` (`worker.py`), the `plan_actions` config branch, the
`ac-plan` / `ac-review` labels, `prompts/plan.txt`, `prompts/review.txt`, and the
`max_turns_plan` split. `ProcessManager.spawn` (`process_manager.py:101`) loses
its dev-vs-plan routing and always targets `run_dev_worker`.

### Behavioral consequence

auto-claude gains the human gate it never had. It stops firing on `ac-implement`
and fires on `ac-dev-ready`. It treats `ac-hitl`, `ac-merged`, `ac-done`, and
`ac-blocked` as terminal — hands off.

Retry semantics move from "re-label by hand" to the loop's attempt counter:
`ac-attempt-1|2|3`, escalating to `ac-blocked` on the third failure.

---

## 3. Ownership: the assignee decides who works an issue

Decided 2026-07-27. **Assignee is the ownership marker.** auto-claude works
issues assigned to `accelevation-bot`; a human's loop works issues assigned to
that human; unassigned issues are worked by nobody.

| Assignee | Worked by |
|---|---|
| `accelevation-bot` | auto-claude |
| `derekneely`, `dvanblaricom` | that person's `/loop` |
| *(unassigned)* | nobody — safe default |

This is not a new mechanism. The loop already works exactly this way and says so
(`agents/loop-pipeline-planner.md:28-41`):

> The pipeline only acts on issues **assigned to the user running the loop**. An
> **unassigned issue is invisible to every loop**, even if labeled.

```bash
gh issue list --assignee @me --state all --limit 200 ...
```

Verified 2026-07-27: `accelevation-bot` exists, is an `Accelevation` org member,
and is assignable on all four repos.

### Why this beats the alternatives

Rejected: sharing `ac-in-progress` with a locking protocol — needs edits to
`loop-pipeline-planner`'s stale-lock logic and still races.

Rejected: repo-level partitioning via a `runner` field in `pipeline.json` — this
is strictly worse than the assignee. It cannot use `pipeline.json` *presence* as
the marker anyway (board sync needs the file in every repo, §4b), and it forces
whole repos to one runner.

The assignee wins on every count:

- **Zero changes to claude-tools.** The loop already ignores non-`@me` issues, so
  it will ignore bot-assigned issues for free.
- **No repo partitioning.** Both runners can operate on `field_admin`
  simultaneously — you take your issues, the bot takes its own.
- **Ownership is visible in the GitHub UI** rather than buried in a config file.
- **Handoff is a reassignment.** Pull work back from the bot by assigning it to
  yourself; hand it over by assigning the bot.
- **`project-sync.mjs` already supports it** via `--assignee`.

`pipeline.json` is still the shared per-repo contract — auto-claude reads
`project`, `prBaseBranch`, `verify`, `test`, and `projectBoard` from it — it just
carries no ownership field.

**Scheduling is auto-claude's, not the loop's** (decided 2026-07-29). The file's
`concurrency` key is parsed into `PipelineConfig` and **deliberately not
honoured**: `field_admin` declares `"concurrency": 1`, but auto-claude is meant
to work several issues at once, and the goal was to align on the *label
taxonomy*, not to reproduce the loop's execution model. `[workers].max_parallel`
in `config.toml` is the single authority on how many workers run at once, with
the rate-limit pause in `ProcessManager.can_spawn` outranking free capacity.
Same treatment as `worktreeBase`. `staleLockHours` and `defaultBranch` are
likewise parsed and unused — `_release_stale_locks` rewinds every lock at
startup rather than ageing them out.

### The handoff protocol

**An issue enters auto-claude's queue when, and only when, both are true:**

1. it carries the trigger label `ac-dev-ready`, **and**
2. it is assigned to `accelevation-bot`.

Both halves are load-bearing and neither is inferred. The label says the work is
*ready*; the assignee says *who* does it. A labelled issue with no bot assignee
is invisible to auto-claude (and, if assigned to a human, is that human's loop's
work). A bot-assigned issue without the label is parked, not queued.

This is a real change in operator burden and worth stating plainly: **labelling
an issue is no longer enough to make it happen.** Assigning to the bot is now a
required, deliberate step. Nothing in auto-claude assigns issues to itself — by
design, since self-assignment would let it reach outside its own queue.

To hand work to the bot:

```bash
gh issue edit <n> --repo Accelevation/<repo> \
  --add-assignee accelevation-bot --add-label ac-dev-ready
```

To take it back mid-flight, reassign it to yourself. The next poll no longer
sees it; a worker already running finishes its current attempt (auto-claude does
not re-check ownership mid-run) and then stops, because re-queueing goes through
the poller.

Implemented in `poller.py:56` → `github_client.list_issues(..., assignee=...)`,
which appends `assignee=<bot_login>` to the API query. Covered by
`tests/test_assignee_filter.py`.

### Authentication: auto-claude runs as the bot

Decided 2026-07-27, implemented 2026-07-28. Of the two options — authenticate as
the bot, versus authenticate as `derekneely` and merely *assign* to the bot —
the first was chosen. It is the only one that delivers "keep it simple as to who
is working on it": every branch, commit, PR and comment is attributed to
`accelevation-bot`, so the GitHub UI separates bot work from human work without
anyone reading a config file.

The plumbing lives in `ghauth.py`. Two things made it non-trivial:

- **The token is `AUTO_CLAUDE_GH_TOKEN`, never `GH_TOKEN`.** A `GH_TOKEN`
  exported in the operator's shell would hijack their own interactive `gh` and
  their own loop. auto-claude translates its private variable into `GH_TOKEN`
  only inside the subprocess environments it builds.
- **`GH_TOKEN` governs `gh` but not `git`.** On Windows `credential.helper=manager`
  is set at *system* scope and holds the human's credentials, so a plain
  `git push` authenticates as the human regardless of the token. Network git
  commands are prefixed with `git_credential_args()`, which resets the inherited
  helper list and substitutes gh's. Without this, attribution is half-broken in
  a way that looks like it works.

A PAT acts as **whoever created it**, regardless of the "Resource owner" setting
— so the token must be generated while signed in as `accelevation-bot`.

### Fail-closed: no identity, no run

Decided 2026-07-28. auto-claude **refuses to start** unless it knows who it is.
Two conditions are fatal, and unlike the repo/permission checks they are *not*
bypassable with `--skip-preflight`:

| Condition | Why fatal |
|---|---|
| no `bot_login` in `[github]` | the poller sends no assignee filter, so every `ac-*` labelled issue in the org is fair game — including ones a human loop has claimed |
| no token, or a token that authenticates as anyone else | `gh` falls back to the operator's stored credentials and the work is committed, pushed and commented under a human's name |

Both were previously warnings. A warning is the wrong severity for an
unattended process: neither is recoverable at runtime, and both fail *silently*
in the sense that the run looks successful — the damage shows up later as a PR
in someone else's name, or two agents on one issue.

`ghauth.check_ownership_config()` is the network-free half (pure config
coherence, runs offline); `ghauth.verify_identity()` is the single `gh api user`
call. `main` runs both before anything else and exits 1 on failure.
`--skip-preflight` now skips only `check_access()` — repo push permission and
the Projects v2 board. Covered by `tests/test_ownership_gate.py`.

Verified 2026-07-28: with a deliberately invalid token, startup aborts with
`[FAIL] identity: gh api user failed - token invalid or unapproved (HTTP 401)`
and exit code 1.

### Base branch: `dev`, not `derekdev`

Decided 2026-07-27 — `derekdev` has caused problems in practice. All four repos
have a `dev` branch (verified 2026-07-27), so this is safe everywhere.

- auto-claude: `[github].base_branch` set to `dev` in `config.toml` (done
  2026-07-27). To be superseded entirely by per-repo `prBaseBranch` in task B.
- **Action required in another repo:** `field_admin/.claude/pipeline.json` has
  `"prBaseBranch": "derekdev"`. It is loop-owned and live — change it
  deliberately at cutover, not as a side effect of this work.

---

## 4. Rate limiting (independent, do first)

Verified 2026-07-27 against Claude CLI 2.1.220 with no `ANTHROPIC_API_KEY` set:

- Headless subscription auth works — `claude --print` returns exit 0.
- Cost accounting is live: the result event carries
  `total_cost_usd` and per-model `modelUsage[].costUSD`. So `--max-budget-usd`
  does meter, and the continuation path at `process_manager.py:149` is real code.
- **The binding constraint is the rate limit, not the budget:**

```json
{"type":"rate_limit_event","rate_limit_info":{
  "status":"allowed","rateLimitType":"five_hour",
  "resetsAt":1785169800,"overageStatus":"allowed","isUsingOverage":false}}
```

`grep -rn "rate_limit\|429\|overage" *.py` → no matches. Nothing handles this.

With `max_parallel = 3` on Sonnet/Opus, the five-hour window is hit long before
any worker spends `max_budget_usd = 10.0`. Today that surfaces as an opaque
worker failure and `process_manager.py:170` marks it FAILED pending a manual
re-label.

Required work:

1. Parse `rate_limit_event` in `_extract_display_text` / the stream loop
   (`worker.py:317`); carry `status`, `resetsAt`, `isUsingOverage` out of `_run_claude`.
2. On `status != "allowed"`, return a distinct `rate_limited` outcome — not a
   generic failure.
3. `ProcessManager.reap_dead` treats `rate_limited` like `budget_exceeded`:
   re-queue rather than fail, but gate on `resetsAt`.
4. Add a global pause: when any worker reports rate limiting, stop spawning
   until `resetsAt`, and log the wake time. This is a supervisor-level
   backpressure signal, not a per-worker one.
5. Surface `isUsingOverage` in logs so overage burn is visible.

### Trap: do not use `--bare`

Each worker pays ~11k cache-creation tokens for hook/skill context, and `--bare`
looks like the fix. It is not. Per its help text, under `--bare` auth is
*strictly* `ANTHROPIC_API_KEY` or `apiKeyHelper`; OAuth and keychain are never
read. It would break the subscription auth this whole design depends on.

---

## 4b. Shared telemetry and board sync

Both decided 2026-07-27: auto-claude reports to the same metrics DB and syncs the
same Projects v2 board as the loop.

Both are Node scripts living in the claude-tools checkout, so auto-claude needs a
new config key pointing at it (the loop gets this free via `CLAUDE_PLUGIN_ROOT`):

```toml
[integrations]
claude_tools_root = "../accelevation/accelevation-claude-tools"
```

**Telemetry** — after every stage transition, fire-and-forget:

```
node <root>/tooling/pipeline-metrics/scripts/log-event.mjs \
  --project <pipeline.json project> --issue <n> --stage <stage> \
  --action <action> --actor auto-claude [--attempt N] [--pr N] [--duration ms]
```

`log-event.mjs` always exits 0 and warns to stderr on failure, so it is safe to
call unconditionally. It needs `PIPELINE_METRICS_DATABASE_URL`, loaded from
`tooling/pipeline-metrics/.env` (gitignored) — run `/accelevation:pipeline-db-config`
once if unset. Use `--actor auto-claude` so both runners are distinguishable in
the same table.

**Board sync** — after each tick:

```
node <root>/commands/scripts/project-sync.mjs --issue <n>
```

Two gotchas:

1. It must run from *inside* the consuming repo (it reads `./.claude/pipeline.json`).
   auto-claude must set `cwd` to the repo checkout, not its own root.
2. It only syncs issues assigned to the authenticated `gh` user by default. Under
   §3 this resolves itself: with auth-as-bot (option a) the default is already
   correct; with assign-only (option b) auto-claude must pass
   `--assignee accelevation-bot`. Either way **unassigned issues silently do not
   sync** — exit 0, no error. Likeliest thing to go quietly wrong.
3. It syncs only *open* issues, so reconcile the card before closing an issue —
   the loop already sequences it this way in `issue-pipeline-tick` Step 4b.

Absent a `projectBoard` block the script exits 0 and does nothing, so repos
without a board are fine.

---

## 5. Work breakdown

Sized as independent sub-tasks.

**A. Rate-limit handling** — ✅ **DONE 2026-07-27.** New `ratelimit.py` (pure
parse + backoff policy, fully unit-tested), `_run_claude` returns the event as a
4th value, dev worker pushes partial work and re-queues on limit *without*
running the handoff summary (that call would hit the same wall) and without
consuming a continuation, `ProcessManager.can_spawn()` gates the whole pool until
the window resets. 40 tests in `tests/`. Verified end-to-end against real CLI
output. The reset buffer is the `DEFAULT_BUFFER_SECONDS = 60` constant in
`ratelimit.py`; promote it to config only if it needs tuning.

**A2. Bot auth plumbing** — ✅ **DONE 2026-07-27.** New `ghauth.py`: token loaded
from `AUTO_CLAUDE_GH_TOKEN` or a gitignored `.gh_token` (deliberately *not*
`GH_TOKEN`, which would hijack the operator's own shell and loop), translated to
`GH_TOKEN` only inside subprocess envs. All five env-construction sites now route
through `build_env()`. Network git commands get `apply_git_credentials()`, which
resets the inherited helper list before substituting gh's — **required**, because
`credential.helper=manager` is set at *system* scope on Windows and would
otherwise push as the operator regardless of the token. Startup preflight in
`main.py` (skippable via `--skip-preflight`) checks identity, per-repo push, and
org Projects access, naming the specific missing grant. Verified end-to-end
against a real network call. `bot_login` added to config.

**A3. Fail-closed identity gate** — ✅ **DONE 2026-07-28.** Preflight split into
three pieces: `check_ownership_config()` (network-free — is `bot_login` set, is a
token loaded), `verify_identity()` (one `gh api user` call), and `check_access()`
(per-repo push + Projects). The first two are **fatal and unskippable**; only
`check_access` is behind `--skip-preflight`. Missing `bot_login` and missing/wrong
token were previously warnings — the wrong severity for an unattended process,
since both fail silently and surface later as a PR under a human's name or two
agents on one issue. Verified live: invalid token → exit 1 with an HTTP 401
detail; valid token under `--skip-preflight` → gate still runs and passes.
`tests/test_ownership_gate.py`.

**B. Config + preflight** — ✅ **DONE 2026-07-28.** New `pipeline.py`:
`load_pipeline_config(repo_root)` returns a `PipelineConfig` or `None`, parsing
`project`, `defaultBranch`, `prBaseBranch`, `verify`, `test`, `concurrency`,
`staleLockHours`, `worktreeBase`, and the optional `projectBoard` block (with an
`is_valid` property, since a board block missing `projectId`/`statusFieldId` is
present-but-useless). Unknown keys are tolerated — the sibling toolchain owns
this schema and may extend it. `[integrations].claude_tools_root` added.
`process_manager` picks `prBaseBranch` per repo, caching the read.

Two deviations from the original plan text, both deliberate:

- **`base_branch` was NOT removed.** It is the fallback for repos with no
  `pipeline.json`, which today is all of them (see blocker below).
- **A missing `pipeline.json` is NOT a hard error.** It warns and falls back.
  Hard-failing would take the daemon down for three of four repos.

**⚠️ Resolved 2026-07-28: read `pipeline.json` from the base branch, not the
default branch.** `_repo_pipeline` called `get_file(repo, path)` with no `ref`,
and GitHub's contents API defaults to the repo's **default branch**. For
`field_admin` that is `main`, which trails the active integration branch `dev`
by a release cycle — so auto-claude read `prBaseBranch: "derekdev"` for days
after `dev` had been corrected to `dev`. The sibling toolchain never hits this
because it reads the operator's working checkout.

`_repo_pipeline` now tries `[github].base_branch` first and falls back to the
default branch, so a repo that keeps the file only on its default branch is
unaffected. `tests/test_pipeline_ref.py`, 9 tests. Verified live 2026-07-28:
`field_admin` resolves `prBaseBranch=dev`; the other three warn and fall back to
the global `base_branch = "dev"`, which is correct.

This also retired `field_admin` PR #301 (`prBaseBranch: derekdev → dev`) — its
branch was already 0 commits ahead of `dev`, so merging it was a no-op. The
change had landed on `dev` all along; only the read path was wrong. Same class
of mistake as the stale-`origin/*` one in the gotchas: the config was fine, the
lookup was not.

**⚠️ Superseded blocker (2026-07-28): `pipeline.json` is not published.**
`field_admin/.claude/pipeline.json` is committed on the **local** `dev` branch
but has never been pushed — `origin/main`, `origin/dev` and `origin/derekdev` all
lack it. auto-claude clones from origin, so it will never see the file, and
neither would any other machine. The loop only works because it runs from a local
checkout. **Until that commit is pushed, every repo uses the global fallback**
(`base_branch = "dev"`), which is correct but not per-repo. The other three repos
have no `pipeline.json` at all and need `/accelevation:pipeline-setup`.

This also supersedes the §3 note about flipping `field_admin`'s `prBaseBranch`
from `derekdev` to `dev`: do it in the same push.

**C. Poller retarget** — `poller.py`.

- ✅ **Assignee scoping DONE 2026-07-27.** `list_issues()` takes an `assignee`
  param (URL-encoded); the poller passes `config.github.bot_login`. Startup logs
  the active scope. Verified live 2026-07-28: across the four repos, unscoped
  discovery returns 117 open issues, bot-scoped returns 0 — previously all 117
  were in range. Omitting `bot_login` is no longer a supported fallback; it is
  fatal at startup (A3).
- ⚠️ **Operational consequence:** the bot's queue is currently empty because no
  issue is assigned to `accelevation-bot`. Assignment is a deliberate human step
  and nothing automates it. See §3 "The handoff protocol".
- ✅ **Stage retarget DONE 2026-07-28.** `_find_action_label` deleted. The
  poller now calls `stages.is_claimable` / `is_terminal` / `kind_of`. Terminal
  stages short-circuit the whole issue before any state read or write, so a
  human setting `ac-blocked` sticks even when local state says FAILED. The
  rework and retry branches are nested under `is_claimable`, which is the
  substance of task D: **local `IssueStatus` is execution history, not
  permission** — only the label can authorize resuming work.
  `tests/test_poller_stages.py`, 19 tests.

**Shared contract: `stages.py`** — ✅ **NEW 2026-07-28.** The single source of
truth for the vocabulary, mirroring `setup-pipeline-labels.sh`. Exposes
`STAGE_LABELS` (ordered), `TERMINAL`, `LOCKED`, `CONTROL_LABELS`, `KIND_LABELS`,
`MAX_ATTEMPTS`, `LABEL_SPECS` (name/colour/description for label creation),
`RETIRED_LABELS`, and the helpers `stage_of`, `is_claimable`, `is_terminal`,
`kind_of`, `attempt_of`, `attempt_label`, `attempts_exhausted`, `transition`.
57 tests. Two decisions worth noting:

- `stage_of` resolves a half-applied transition (two stage labels at once) to
  the **earliest** stage, so a crash mid-swap re-runs rather than skipping
  forward.
- `transition` removes *every* other stage label rather than just the expected
  predecessor, so a half-applied transition self-heals. It never removes kind
  hints, control labels, or unrelated labels.

**D. State model** — ✅ **DONE (scoped down) 2026-07-28.** The full "make
`state/issues.json` a pure cache over labels" rewrite was **not** done and is not
scheduled. What was actually needed — and what shipped — is that the label wins
wherever the two disagree, enforced at the poller's branch points. `IssueStatus`
remains the local execution state (what *this process* is doing); the labels are
the pipeline state (what the *issue* is doing). Two stores with distinct jobs is
fine; two stores claiming the same job was the bug.

**E. Worker labeling** — ✅ **DONE 2026-07-28.** Three pure functions
(`_labels_for_claim` / `_labels_for_success` / `_labels_for_failure`) compute
add/remove sets via `stages.transition`; thin wrappers read the issue's **live**
labels from GitHub at the point of transition rather than trusting the snapshot
taken at spawn (labels change mid-run, and `transition`'s self-healing only works
against the actual current set). Claim → `ac-in-progress`; success →
`ac-dev-review` + `ac-pr-created`; failure → `ac-dev-ready` with the attempt
counter bumped, or `ac-blocked` on the third. 12 tests.

Fixed a real bug in passing: `_push_rework` removed `ac-rework` on success,
stripping the kind label off every successfully reworked issue.

**⚠️ Divergence from the loop, deliberate.** The loop's dev agent does *not*
touch `ac-attempt-N` — its review agent owns the counter. auto-claude's worker
bumps its own, because nothing else will: the loop's review agent scopes itself
with `--assignee @me` and so never sees a bot-assigned issue. Without a local
bump, a failing issue would retry forever.

**⚠️ Open design gap: nothing reviews auto-claude's PRs.** The worker hands off
at `ac-dev-review`, which in the loop means "awaiting agent review" — but the
review agent only picks up issues assigned to the human running it. A
bot-assigned issue will therefore park at `ac-dev-review` indefinitely. Options,
undecided: (a) set `ac-hitl` instead and let a human review directly;
(b) reassign to a human once the PR is open; (c) give auto-claude its own review
worker; (d) run a loop instance authenticated as the bot. (a) is a one-line
change and the cheapest way to make the pipeline complete today.

**Stale-lock recovery** — ✅ **NEW 2026-07-28.** `main._release_stale_locks()`
runs at startup: any bot-assigned issue still carrying `ac-in-progress` is stale
by definition, because no worker of ours survives a restart — no timeout needed.
Rewinds via `stages.STALE_RESET` (`ac-in-progress` → `ac-dev-ready`,
`ac-review-in-progress` → `ac-dev-review`), matching the sibling planner's
mapping. This existed nowhere before: the loop's stale sweep only covers issues
assigned to the human running it, so a crashed auto-claude worker would have
removed an issue from circulation permanently and silently.

**F. Label stamping** — reuse `accelevation-claude-tools/commands/scripts/setup-pipeline-labels.sh`
on the three auto-claude-owned repos rather than writing a second stamper.

**G. Migration** — ❌ **NOT NEEDED.** Verified 2026-07-28: across all four repos,
**zero** open issues carry any `ac-*` label. There is nothing in flight to
relabel, so the one-shot migration script is cancelled. The §2 mapping table
stands as documentation of what the old labels *meant*, not as a migration to
run. Retired labels are listed in `stages.RETIRED_LABELS`; they are left on the
repos rather than deleted (deleting a label is destructive and org-wide) and are
simply never written again.

If issues are labelled with the old vocabulary before cutover completes, re-check
this and reinstate the task.

**F2. Retire the plan/review worker** — ✅ **DONE 2026-07-28.** Deleted
`run_plan_worker` (worker.py shrank 1350 → 1225 lines), `prompts/plan.txt`,
`prompts/review.txt`. `ProcessManager.spawn` now targets `run_dev_worker`
unconditionally. Dropped from config: `plan_actions`, `max_turns_plan`,
`plan_posted_label`, `review_posted_label`, and the `ac-plan` / `ac-review`
entries in `action_labels` and `[claude.action_models]`. Dropped from `state.py`:
the `PLANNING` and `PLAN_POSTED` statuses and their transitions. Dropped the
plan→action re-dispatch branch in `poller.py` and the `PLANNING` spawn loop in
`main.py`. `GithubConfig(**raw["github"])` means a stale key in an old
`config.toml` now fails loudly at load rather than being ignored. README label
table corrected. Verified: 127 tests pass, all modules import, and a live
`--dry-run` boots green through preflight to the poll loop.

Also fixed in passing: `_ensure_labels` ran *before* the dry-run check, so
`--dry-run` created labels on GitHub. It is now skipped under `--dry-run`.

**I. Telemetry + board sync** — ✅ **DONE 2026-07-28.** New `integrations.py`
wrapping both Node scripts. `log_event(TelemetryEvent(...), root)` emits
`--actor auto-claude` (the discriminator against the loop's `loop-dev-agent`);
called from the worker's three transition points as `picked_up` / `pr_opened` /
`blocked`|`review_fail`. `sync_board(cwd, root, ...)` runs once per repo per
poll tick from `main`. `project-sync.mjs` reads a literal relative
`.claude/pipeline.json`, so cwd must be a directory holding that file — see the
correction below for where that directory comes from. 26 + 17 tests.

**Correction 2026-07-29 — board sync no longer uses the repo checkout.** cwd was
originally `repos/<repo>`, which is wrong for a reason that took a live run to
surface: that checkout is shared with the workers and sits on whatever branch one
of them last left it on. `repos/field_admin` was cloned 2026-04-16 and sat on
`main` until the first real worker ran; `.claude/pipeline.json` exists **only on
`dev`** (`git merge-base --is-ancestor 4b6a322 main` → false). So every sync
exited 1 with `pipeline.json not found` until the dev worker for #215
incidentally ran `git checkout dev` — 16 seconds after the failed sync.

`_sync_boards` now fetches the file from GitHub (base branch first, then the
default branch — the same rule `ProcessManager.pipeline_for` follows and for the
same reason) and writes it into a `tempfile.TemporaryDirectory`, which becomes
cwd. cwd is the script's only filesystem dependency: every `gh` call it makes
either passes `--repo`, which auto-claude always supplies, or is
`gh api graphql`. Consequences: board sync no longer depends on checkout state,
cannot race a worker mutating that checkout, and works for a repo that has never
been cloned — so the `repo_root.is_dir()` skip is gone. Costs one API call per
repo per tick; deliberately uncached so a `pipeline.json` edit is picked up
without a restart. `TestSyncBoards` went 3 → 8 tests; the two that pinned
"cwd is the local checkout" and "skip repos that are not cloned" encoded the bug
and were removed.

Both are unconditionally non-fatal: every exception, `FileNotFoundError` (no
`node`), and timeout is swallowed. Verified 2026-07-28 that `log-event.mjs` with
no DB configured prints `warn: pipeline metrics write failed:
PIPELINE_METRICS_DATABASE_URL is not set` and **exits 0**.

**⚠️ Telemetry is inert until the DB is configured.** There is no
`tooling/pipeline-metrics/.env` and no `PIPELINE_METRICS_DATABASE_URL`. Run
`/accelevation:pipeline-db-config` then `/accelevation:pipeline-db-migrate`.
Until then every event is dropped — safely, but silently.

**H. Docs** — ✅ **DONE 2026-07-28.** `README.md` label table rewritten around the
two-part trigger contract. The MVP design docs (`00`, `02`, `04`, `06`, `08`,
`09`) carry a "superseded in part" banner pointing here rather than being
rewritten — their *architecture* is still accurate; only the label and state
details are history, and rewriting them would create a second source of truth to
keep in sync.

**J. Review worker** — auto-claude reviews its own PRs, because the sibling
toolchain's review agent scopes itself with `--assignee @me` and will never see a
bot-assigned issue (see the design gap under task E). Mirrors
`agents/loop-review-agent.md`: self-lock `ac-dev-review` → `ac-review-in-progress`,
check out the PR branch, run `verify` + `test` from `pipeline.json`, review the
diff (including a security pass) via the Claude CLI, then either approve and set
`ac-hitl`, or request changes, bump `ac-attempt-N` and return to `ac-dev-ready` —
escalating to `ac-blocked` on the third failure.

`IssueRecord.mode` (`"dev"` | `"review"`) selects the worker; the poller sets it
from the stage label so routing follows the label rather than a local guess.
Review records skip triage: "is this issue specified well enough to implement" is
meaningless for a PR review, and a needs-info verdict would strand an open PR at
`ac-input-needed`.

- ✅ **Poller + routing DONE 2026-07-28.** `stages.is_reviewable()`; the poller
  discovers `ac-dev-review` issues straight to `QUEUED` with `mode="review"`, and
  re-marks `mode` on every path so an issue swinging between stages never spawns
  the wrong worker. `process_manager.spawn` routes on `mode`. `main` skips triage
  for review records, in both the poll loop and `--issue` mode. 32 poller tests.
- ⬜ `run_review_worker` itself — in progress.

**⚠️ The review worker must find its own PR.** `IssueRecord.branch` / `pr_url` are
only populated when *the same daemon process* ran the dev worker earlier and got a
`StateUpdate` back. They are `None` after any restart, when the sibling toolchain
opened the PR, or when a human applied the label — i.e. most of the time. The
review worker therefore looks the PR up from the issue number (branch convention
`ac/issue-<N>-*`, else a closing reference in the PR body) and, finding none,
must refuse to approve rather than pass vacuously.

**K. Push guard** — ✅ **DONE 2026-07-28.** `worker.assert_pushable(branch,
base_branch)` raises `ProtectedBranchError` before every `git push`, refusing
`main` / `master` / `dev` / `develop` / `trunk` / the repo's configured
`prBaseBranch`, plus the unusable values (`""`, whitespace, `None`, `HEAD`) that
make git fall back to the push default. Comparison is normalised — lowercased,
trimmed, `refs/heads/` stripped — and exact, so `ac/issue-9-fix-dev-server-crash`
and `dev-tools-refactor` still push fine.

Wired at all three push sites: `_push_and_pr` and `_push_rework` raise before
staging anything; `_push_partial_work` logs and returns `None`, preserving its
best-effort contract (every other failure in that function returns `None` too).
24 tests in `tests/test_push_guard.py`, including a meta-test that counts
`git push` sites against `assert_pushable` calls so a new push path fails CI
until it is guarded.

**Why it exists.** Two of the three push sites do not compute the branch name —
`_setup_rework_worktree` (`worker.py:845`) reads it from `state/issues.json`,
and `run_review_worker` (`worker.py:1774`) reads it from a PR's `headRefName`.
A stale state file, or simply a PR opened with `dev` as its head, reaches
`git push origin dev` with no other check in the way. Server-side branch
protection is unavailable (§3 — free plan, private repos) and `accelevation-bot`
holds `write`, so nothing on GitHub's side would have stopped it. This closes
the last item under "Open questions".

**L. Orchestrator framing + issue write-back** — ✅ **DONE 2026-07-28.** Two
gaps, both invisible from outside.

The worker's agent was a flat coder: `develop.txt` told it not to explore and
never mentioned that a headless `claude --print` sees the entire
`accelevation:*` plugin roster (verified — all 14 are available). The "varying
models" the design called for existed only as Python picking one model per
action. `prompts/_orchestration.txt` now frames the agent as an orchestrator
and names the roster. It is one shared file injected as `{orchestration}` into
`develop` / `test` / `continue` / `rework`, because a template that drifts out
of sync silently reverts to a flat coder and nothing would catch it.

**Only advisory agents are offered.** `accelevation:dev-agent`,
`review-agent`, `triage-agent`, `pipeline-planner` and `test-pr-agent` are in
an explicit never-delegate list: each self-locks `ac-*` labels, claims its own
worktree slot, and opens its own PR. Delegating to one from inside a worker
puts two systems on one issue — the exact collision §1 exists to remove. A test
asserts all five stay named, so wiring one in means deleting the test that says
not to.

Write-back: the issue was read as source of truth (body and comments reach the
prompt) but only ever received `PR created: <url>`. The agent now emits
`IMPLEMENTATION_PLAN` / `_SUMMARY` / `_NOTES` and `_post_issue_report` posts
them as one structured comment on success — non-fatal, since failing a worker
that already opened a PR would re-queue a completed issue and produce a second
one. 27 tests in `tests/test_issue_report.py`.

**Labels stay in Python** (decided 2026-07-28). The agent is told where it sits
in the pipeline but does not drive it: a crashed or confused agent cannot
strand an issue mid-stage, and stale-lock recovery stays trivial.

**M. Worktree preparation + pre-push validation** — ✅ **DONE 2026-07-28.**
Found by asking why the dev worker never tested anything. Two defects, the
second one fatal.

`_run_pipeline_checks` was called from exactly one place — `run_review_worker`.
`run_dev_worker` went agent → detect changes → push → PR, with no build gate at
all. A break cost a full extra dev cycle and an attempt to discover.

Worse: **nothing prepared the worktree.** `git worktree add` yields tracked
source and nothing else — no `node_modules`, no generated Prisma client, no
gitignored env. Reproduced against the real clone:

```
> nextn@0.1.0 typecheck
> tsc --noEmit
'tsc' is not recognized as an internal or external command
```

Every `field_admin` PR would therefore have failed review on a missing
toolchain, three times, landing at `ac-blocked` with feedback blaming the code.
The sibling toolchain never hits this because `test-pr-agent` does the setup by
hand; an unattended daemon has to do it itself.

New `worktree_setup.py`: `detect_setup_commands` infers from the checkout
(`package.json` → `npm install`; each depth-1 sibling package → `npm --prefix
<dir> install`; `prisma/schema.prisma` → `npx prisma generate`;
`requirements.txt`/`pyproject.toml` → pip; `gradlew` → nothing, it bootstraps
itself). Overridable per-repo via `[repos.<name>]` in auto-claude's *own*
config — deliberately not a new key in `pipeline.json`, whose schema the
sibling toolchain owns.

The depth-1 rule is load-bearing, not incidental: `field_admin`'s root
`tsconfig.json` includes `**/*.ts` and excludes only `node_modules`, so it
typechecks `functions/src` — whose dependencies live in
`functions/package.json`. Root-only install left `Cannot find module
'firebase-functions/v2/firestore'`, which reads as a code error.

Both workers now go through `_prepare_and_check`. The dev worker runs it
**before pushing**; on failure the agent gets one in-session repair round
against the failure transcript, then a re-check. Still red → nothing is pushed,
the transcript goes on the issue, and the attempt is bumped. The review worker
becomes a second opinion rather than the first thing that ever compiles the
code.

**Dev credentials only.** `.env.production` is deliberately not copied. Its
`DATABASE_URL` points at the production database, and the worktree is where an
agent runs under `bypassPermissions` — nothing would stop it running a
migration or a query against prod. Both env files *are* gitignored in
`field_admin`, so the worker's `git add -A` could never have committed them;
the risk was the agent's own reach, not the commit.

Copying only `.env.development` does not work either: `next build` sets
`NODE_ENV=production`, so Next loads `.env.production` and ignores
`.env.development` entirely. The build fails with `DATABASE_URL is not set`.
`.env.local` loads in *every* mode, so `env_file_as` lands the dev file there —
`{ ".env.development" = ".env.local" }`. The build gets a **dev** DATABASE_URL
and production credentials never enter the worktree.

Verified end-to-end on fresh `origin/dev` worktrees at each step: prod env
present → build green (2m13s); dev env only → build **fails**, `DATABASE_URL is
not set`; dev env landed as `.env.local` → typecheck exit 0, build exit 0
(1m39s), and a grep for the production DATABASE_URL across the worktree finds
nothing. 57 tests in `tests/test_worktree_setup.py`.

### Sequencing

- ~~**A** (rate limits), **A2** (bot auth), **A3** (fail-closed gate)~~ ✅
- ~~**F2** (retire plan worker)~~ ✅
- ~~**B → C → D → E**~~ ✅ — the core is done. 288 tests.
- ~~**I** (telemetry + board sync)~~ ✅ — wired, inert until the metrics DB exists.
- ~~**K** (push guard)~~ ✅ — the only protection against a runaway push to a
  shared branch while server-side rulesets are unavailable.
- ~~**G** (migration)~~ ❌ cancelled — nothing in flight to migrate.
- **F** (label stamping) — ✅ code done (`stages.LABEL_SPECS`, create-only, runs
  at startup); ⬜ not yet executed against GitHub. First non-dry-run start will
  create the missing labels: `field_admin` needs 4, the other three need 14 each.
- **H** (docs) — ⬜ README label table updated; `docs/plans/02`, `06`, `09` still
  describe the pre-consolidation design.

### Scope: one repo at a time, gated on verify commands

Decided 2026-07-28. `[github].repos` is narrowed to **`field_admin` only**. A
repo joins the list when its `.claude/pipeline.json` declares *real* verify/test
commands, not merely when the file exists.

The reason is `_run_pipeline_checks`: a repo with no `pipeline.json`, or one
with empty `verify` and `test`, has nothing to run — so it returns `ok=True`
and the review worker approves on the diff alone. That is deliberate (failing
would strand every legitimately-unbuildable repo at `ac-blocked`) and it is
honest (the transcript says so, and it reaches the review prompt). But an
approval with no build gate behind it should be an explicit choice per repo,
not a side effect of listing it here. Listing a repo also stamps the ac-* label
set on it at startup, so an unconfigured repo is not a free addition.

Held back as of 2026-07-28:

| Repo | Why |
|---|---|
| `QualityFieldApp` | PR #110 merged, resolves `./gradlew :app:assembleDebug` + `:app:testDebugUnitTest`. Task names verified against `settings.gradle.kts`, never executed. A cold gradle build may exceed `_run_pipeline_command`'s 600s timeout. |
| `quality-field-agent` | PR #2 still open — no `pipeline.json` at all. |
| `quality-field-documentation` | PR #4 still open, and declares `verify: []` / `test: []`, so merging it would not add a gate. |

`field_admin` itself declares `test: []`, so review runs typecheck + build and
never runs tests. Worth revisiting, but it is a real gate.

### Blocking a first real test

1. **Assign an issue to `accelevation-bot` and label it `ac-dev-ready`.** Nothing
   is in the queue — 117 open issues, 0 assigned to the bot. Both halves are
   required; see §3.
2. **Decide the `ac-dev-review` hand-off** (task E, open design gap) — otherwise
   the first successful run parks and nothing picks it up.

Neither blocks starting the daemon; both block a *complete* pass.

---

## 6. Decisions log

Resolved 2026-07-27:

1. **Plan/review worker: retired.** `loop-triage-agent` subsumes it. (§2)
2. **Telemetry: yes** — auto-claude writes to the shared metrics DB with
   `--actor auto-claude`. (§4b)
3. **Board sync: yes** — auto-claude runs `project-sync.mjs`. (§4b)
4. **Base branch: `dev`**, not `derekdev`, via per-repo `prBaseBranch`. (§3)

5. **Ownership: the assignee**, not a repo-level `runner` field. auto-claude
   works `accelevation-bot`'s issues; humans' loops work their own. Needs no
   changes to claude-tools. (§3)

Resolved 2026-07-28:

6. **auto-claude authenticates *as* `accelevation-bot`** (§3 option a), not as
   `derekneely` assigning to the bot. Bot PAT created and working. (§3)
7. **Fail closed on identity.** No `bot_login` or no valid bot token is a fatal
   startup error, not a warning, and `--skip-preflight` does not bypass it. (§3)
8. **The trigger contract is `ac-dev-ready` + assigned to `accelevation-bot`** —
   both required. Labelling alone no longer queues work. (§3)

Still open:

- None blocking.
