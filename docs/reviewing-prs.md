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
                                 -> ac-merged -> ac-done
```

`ac-hitl` is in `stages.TERMINAL` — auto-claude **stops touching the issue**
once it lands there. It is not a failure state; it means the agent review
passed (build green, acceptance criteria met, no blocking security finding)
and the PR is waiting on you. From `ac-hitl` onward, `ac-merged` and `ac-done`
are set by a human, not by the daemon.

So: **anything at `ac-hitl` is your queue.**

```powershell
gh issue list --repo Accelevation/field_admin --assignee accelevation-bot `
  --label ac-hitl --state open
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

From the `field_admin` checkout:

```
/accelevation:test-pr 215
```

That builds a throwaway worktree at `../field_admin-test-215` on branch
`test/issue-215`, merges the integration branch, copies the gitignored `.env*`
files, runs `npm install` + `prisma generate`, and starts `next dev` on a
non-9002 port so it does not collide with your own dev server. Then walk the
PR body's **How to test** list.

When you are done:

```
/accelevation:test-pr-cleanup 215
```

This does not merge anything. It only tears down the worktree, the dev server,
and the local `test/issue-215` branch.

## Accept or send back

**Accept** — merge in the GitHub UI or:

```powershell
gh pr merge 334 --repo Accelevation/field_admin --squash
```

The body ends with `Closes #<n>`, so merging closes the issue. Move the stage
label to `ac-merged`, then `ac-done` once it is released.

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
