# Reviewing auto-claude's pull requests

## Why there is no `derekdev`

A pull request targeting `dev` puts **nothing** into `dev`. The branch sits
outside `dev` until someone clicks merge, and that click is the gate. Adding a
second integration branch would not add protection — it would add a branch to
keep in sync.

It would also change where agents *start* from. `base_branch` is one knob for
both ends: `worker.py` uses it as the branch source (`origin/<base>`) and as
the PR base (`--base`). Pointing it at `derekdev` would make every agent branch
off the unreviewed staging branch, so their work would stack on top of code you
had not accepted yet, and `derekdev` would drift from `dev`.

Per repo, `.claude/pipeline.json`'s `prBaseBranch` overrides the global
`[github].base_branch` (`process_manager.py:210`).

## Where your review happens: `ac-hitl`

The stage machine is:

```
ac-pending-review -> ac-dev-ready -> ac-in-progress -> ac-dev-review
                                 -> ac-review-in-progress -> ac-hitl
                                 -> ac-merged (automatic on PR merge) -> ac-done
```

`ac-hitl` is in `stages.TERMINAL` — auto-claude **stops touching the issue**
once it lands there. It is not a failure state; it means the agent review
passed (build green, acceptance criteria met, no blocking security finding)
and the PR is waiting on you. Once you merge the PR, auto-claude's poller
notices within one poll interval and moves the issue to `ac-merged`
("Pending Release") itself — you do not set that label. Only `ac-done`, at
release time, is set by a human.

If a PR tied to an `ac-dev-review` or `ac-hitl` issue is closed **without**
merging, the poller logs one warning and leaves the issue at its current
stage — it does not guess, so a human decides what happens next.

So: **anything at `ac-hitl` is your queue.**

```powershell
gh issue list --repo Accelevation/field_admin --assignee accelevation-bot `
  --label ac-hitl --state open
```

## Finding the PR from the issue

The issue's **Development** panel shows the work branch, and the branch carries
its pull request and that PR's state — which is how you check from the issue
alone whether the work merged.

This does not come from `Closes #N`. GitHub only records a closing reference
when the pull request targets the repository's **default** branch, and every
auto-claude PR targets `base_branch` (`dev`). PR #334 proved it: its body says
`Closes #215`, and `closingIssuesReferences` on the PR is empty — issue #215
showed neither the branch nor the PR.

So `worker._link_branch_to_issue` calls the `createLinkedBranch` mutation
immediately before `git push`. That mutation *creates* the ref, which is why it
has to run first; it creates it at the fork point, so the push that follows is
still a fast-forward. It is best-effort — a failure logs a warning and the run
continues, because linkage is metadata and the work is already done.

Two consequences worth knowing:

- **Merging into `dev` will not close the issue**, `Closes #N` or not, and
  nothing in auto-claude ever calls the close API. The poller advances the
  label to `ac-merged` automatically once it sees the PR merged. Setting
  `ac-done` and closing the issue at release time both stay manual, human
  steps.
- To check merge state from an issue number:

  ```powershell
  gh api graphql -f query='{repository(owner:"Accelevation",name:"field_admin"){
    issue(number:215){linkedBranches(first:10){nodes{ref{name
      associatedPullRequests(first:5){nodes{number state}}}}}}}}'
  ```

## The fast path — read it

```powershell
gh pr view 334 --repo Accelevation/field_admin          # body: summary, changes, how to test
gh pr diff 334 --repo Accelevation/field_admin          # the diff
gh pr diff 334 --repo Accelevation/field_admin --name-only
```

Every PR body now carries a **How to test** section written by the agent that
made the change — a numbered click-through with a starting point, the exact
input to use, and the observable result at each step. Start there. If that
section is vague or missing, that is itself a review finding: send it back.

## The real path — run it

The primary `accelevation/field_admin` checkout is usually occupied by other
agents, so PR testing does not happen there. There is a **persistent worktree**
reserved for it:

```
auto-claude\worktrees\field_admin\pr-testing
```

Point it at any PR:

```powershell
.\scripts\use-pr.ps1 -Pr 334
```

That resolves the PR's head branch, fetches, checks it out **detached**, merges
`origin/dev` on top so you are testing the post-merge state, refreshes
`.env.local`, and reinstalls dependencies. A merge conflict stops the script
and reports it — that is a real review finding, not a script failure.

Then start it on a port that will not collide with your own dev server:

```powershell
cd ..\auto-claude\worktrees\field_admin\pr-testing
npx next dev --turbopack -p 9010
```

`npm run dev` is hardcoded to `next dev --turbopack -p 9002`, so appending
another `-p` would pass the flag twice — invoke `next` directly instead.

Now walk the PR body's **How to test** list.

### Why detached, and why it is safe to live there

The worktree is on a detached HEAD because a branch checked out in one worktree
cannot be checked out in another, and the daemon's clone already holds `dev`.
Detached sidesteps that and makes the state obviously throwaway.

It is safe to sit inside the daemon's `worktrees/` directory: every `rmtree` in
`worker.py` targets a specific `issue-<n>` or `issue-<n>-review` path, and
`git worktree prune` only unregisters worktrees whose directory is already
gone. Nothing sweeps the parent directory.

Only `.env.development` is copied in, landing as `.env.local`. `.env.production`
is deliberately never copied — its `DATABASE_URL` points at the production
database.

### Removing it

It is disposable. To reclaim the disk:

```powershell
git -C repos\field_admin worktree remove --force ..\..\worktrees\field_admin\pr-testing
```

Recreate it with:

```powershell
git -C repos\field_admin worktree add --detach ..\..\worktrees\field_admin\pr-testing origin/dev
```

### The alternative: the throwaway skill

`/accelevation:test-pr 215` does the same job as a one-off — it builds
`../field_admin-test-215` on branch `test/issue-215` and tears it down with
`/accelevation:test-pr-cleanup 215`. Use it if you want two PRs up at once;
use the persistent worktree for everyday review, since it skips a full
`npm install` from cold each time.

## Accept or send back

**Accept** — merge in the GitHub UI or:

```powershell
gh pr merge 334 --repo Accelevation/field_admin --squash
```

Merging does **not** close the issue, `Closes #<n>` or not — see above. There
is nothing left to do here: auto-claude notices the merge on its next poll
(within one interval) and moves the issue to `ac-merged` itself. Set
`ac-done` yourself once the change is released.

**Send back** — comment what is wrong and set the issue to `ac-dev-ready` with
the attempt label bumped. The poller picks it up and a dev worker reworks the
*existing* branch (so the PR updates in place) as long as the issue's state row
still has its `branch` and `pr_url`. Three failed attempts lands it on
`ac-blocked`, which is human-only.

Note: label edits and `gh issue edit` are blocked by the permission classifier
inside a Claude Code session — run them yourself with the `! <command>` prefix,
or from a normal terminal.

## Optional: make the gate enforced rather than conventional

Nothing currently *stops* a merge to `dev` without review. If you want the
convention enforced, add branch protection on `dev` requiring one approving
review and a passing status check. Worth knowing: PR #334 had an empty
`statusCheckRollup` — **field_admin has no CI checks wired**, so today the
agent's own verify commands are the only build gate that ever runs. Issue #154
("Stand up CI pipeline") covers that.
