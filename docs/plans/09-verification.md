# Verification & Testing Plan

## Unit Tests

| Area | What to Test |
|------|-------------|
| Config | Load valid TOML, handle missing sections with defaults, path resolution |
| State | All valid transitions, reject invalid transitions, atomic save/load round-trip, corruption recovery |
| GitHub Client | Error handling on non-zero exit, PR filtering (exclude pull_request items), JSON parsing |

## Integration Tests

### Test 1: Poller Discovery
1. Create a test issue on one of the repos with label `ac-test`
2. Run the poller once
3. Verify issue appears in state as `discovered`
4. Run poller again — verify it's NOT discovered twice

### Test 2: Triage
1. Create an issue with clear description + `ac-fix` label
2. Run triage on it
3. Verify `decision == "proceed"`
4. Create a vague issue ("something is broken")
5. Run triage → verify `decision == "needs_info"` with questions

### Test 3: Full Worker Lifecycle
1. Create issue with enough detail
2. Manually invoke worker with IssueContext
3. Verify: repo cloned, worktree created, Claude invoked, branch pushed, PR created, issue commented

### Test 4: Parallel Workers
1. Create 2-3 issues simultaneously with `ac-fix` labels
2. Start auto-claude
3. Verify multiple colored worker logs interleave in terminal
4. Verify all PRs created

### Test 5: Graceful Shutdown
1. Start auto-claude with an active worker
2. Press Ctrl+C
3. Verify: worker gets abort signal, state saved as `interrupted`, worktree preserved
4. Restart auto-claude
5. Verify: interrupted issue re-queued

### Test 6: Planning Workflow
1. Create issue with `ac-plan` label and a feature description
2. auto-claude triages → proceeds → spawns plan worker
3. Verify: plan posted as comment on issue, `ac-plan-posted` label added, `ac-plan` removed
4. Verify: no code changes, no branches, no PRs
5. Review the plan comment, then add `ac-fix` label to the issue
6. Wait for next poll cycle
7. Verify: issue re-enters flow, triages, spawns dev worker, produces PR

### Test 7: Needs-Info Cycle
1. Create vague issue
2. auto-claude triages → posts questions → adds `ac-needs-info` label
3. Reply to questions on the issue
4. Wait for next poll cycle
5. Verify: re-triage occurs, issue queued for development

## Manual Smoke Test Checklist

- [ ] `python main.py` starts, prints banner, shows config summary
- [ ] Polls all 4 repos without errors
- [ ] Correctly ignores issues without `ac-*` labels
- [ ] Correctly ignores already-tracked issues
- [ ] Triage produces structured JSON response
- [ ] Worker creates worktree in correct location
- [ ] Worker's Claude output streams with color tags
- [ ] PR title and body reference the issue
- [ ] `ac-in-progress` label added during work
- [ ] `ac-pr-created` label added after PR
- [ ] Ctrl+C shuts down cleanly within grace period
- [ ] Log file written to `logs/auto-claude.log` without ANSI codes
- [ ] State file survives restart
- [ ] `ac-plan` label triggers plan-only mode (no PR, plan comment posted)
- [ ] `ac-plan-posted` label added after plan posted
- [ ] Relabeling plan_posted issue with `ac-fix`/`ac-implement`/`ac-test` triggers implementation
- [ ] `ac-review` label triggers review-only mode (no PR, review comment posted)
- [ ] `ac-review-posted` label added after review posted
- [ ] `ac-test` label triggers test writing + running, creates PR with tests
