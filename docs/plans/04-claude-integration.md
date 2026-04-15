# Claude CLI Integration

## Three Modes of Claude Usage

### 1. Triage (fast, cheap, structured output)

**Purpose**: Evaluate if an issue has enough info for automated development.

```bash
claude \
    --print \
    --output-format json \
    --model claude-haiku-4-5 \
    --no-session-persistence \
    "{triage prompt}"
```

**Key flags**:
- `--print`: Non-interactive, returns result and exits
- `--output-format json`: Returns `{"result": "...", "session_id": "..."}` — we parse `result`
- `--model claude-haiku-4-5`: Fast and cheap for classification
- `--no-session-persistence`: Don't save session state

**Expected response** (Claude outputs this as the `result` field):
```json
{
    "decision": "proceed",
    "confidence": "high",
    "summary": "Clear bug report with reproduction steps",
    "questions": []
}
```

Or:
```json
{
    "decision": "needs_info",
    "confidence": "low",
    "summary": "Missing reproduction steps",
    "questions": [
        "What version of the app are you running?",
        "Can you provide steps to reproduce?"
    ]
}
```

### 2. Planning (read-only analysis, posts plan to issue)

**Purpose**: Analyze the repo and issue, produce a structured implementation plan for human review.

```bash
claude \
    --print \
    --output-format stream-json \
    --model claude-sonnet-4-6 \
    --no-session-persistence \
    "{planning prompt}"
```

**Key flags**:
- `--model claude-sonnet-4-6`: Needs strong reasoning for architecture/planning
- No `--permission-mode bypassPermissions` — this is read-only, Claude reads code but makes no changes
- Run with `cwd` set to the repo clone (not a worktree — no branch needed)

**Planning prompt template**:
```
You are a planning agent. Review the following GitHub issue and the codebase
to produce a detailed implementation plan.

Issue #{number} in {org}/{repo}: {title}

Issue Body:
{body}

Analyze the codebase and produce a plan covering:
1. **Summary**: What needs to change and why
2. **Files to modify**: List each file with what changes are needed
3. **New files** (if any): What they contain and why they're needed
4. **Dependencies**: Any new packages or config changes required
5. **Testing approach**: How to verify the changes work
6. **Risks / considerations**: Edge cases, breaking changes, migration needs
7. **Estimated complexity**: Low / Medium / High

Format the plan in clean markdown suitable for posting as a GitHub comment.
```

**Output**: The full text is posted as a comment on the issue, then the action label is replaced with the posted label (`ac-plan-posted` or `ac-review-posted`). For plans, the issue sits in `plan_posted` state until the user reviews and relabels with a dev action label (`ac-fix`, `ac-implement`, `ac-test`) to trigger implementation.

### 3. Development (long-running, full agent capabilities)

**Purpose**: Actually implement the fix/feature in the worktree.

```bash
claude \
    --print \
    --output-format stream-json \
    --model claude-sonnet-4-6 \
    --permission-mode bypassPermissions \
    --max-budget-usd 5.0 \
    --no-session-persistence \
    "{dev prompt}"
```

**Key flags**:
- `--output-format stream-json`: Streams progress as JSON lines — we capture and log each line
- `--model claude-sonnet-4-6`: Most capable model for actual coding
- `--permission-mode bypassPermissions`: Allows file edits, bash commands without prompts
- `--max-budget-usd 5.0`: Safety cap per issue
- Run with `cwd` set to the worktree directory

**Streaming**: Worker uses `subprocess.Popen` with `stdout=PIPE`, reads line-by-line, prefixes each line with the worker's color tag, and forwards to the log queue.

## Triage Prompt Template

```
You are a triage agent for an automated development system. Your job is to
evaluate GitHub issues and determine if they contain enough information for
an AI coding agent to implement the requested change.

You MUST respond with ONLY valid JSON matching this schema:
{
    "decision": "proceed" | "needs_info",
    "confidence": "high" | "medium" | "low",
    "summary": "Brief explanation",
    "questions": []
}

Guidelines:
- "proceed" if: clear description, identifiable code area, well-defined scope,
  >70% confident an AI agent can complete this
- "needs_info" if: vague, missing critical details, unclear scope
- Max 3 questions, specific and actionable
- Respond with ONLY the JSON object
```

## Development Prompt Template

```
Issue #{number} in {org}/{repo}: {title}

Action: {action} (from label ac-{action})

Issue Body:
{body}

Please implement the requested {action}. Focus on:
1. Understanding the codebase structure before making changes
2. Making targeted, minimal changes
3. Running tests if a test command exists
4. Following existing code style and patterns

When complete, output a line starting with IMPLEMENTATION_SUMMARY:
followed by a one-sentence description of what you did.
```

The `IMPLEMENTATION_SUMMARY:` marker is captured by the worker and used as the PR description.

## Error Handling

| Error | Response |
|-------|----------|
| Claude returns non-JSON for triage | Retry once, then default to `needs_info` |
| Claude exits non-zero | Worker marks issue `failed`, eligible for retry |
| Claude times out | Worker catches `TimeoutExpired`, marks `failed` |
| Claude rate limited | Caught as non-zero exit, retried after `retry_delay_seconds` |
| Budget exceeded | Claude exits, worker captures error, marks `failed` |

## Future: Multi-Agent Routing

The architecture supports routing issues to different agents:
- **ollama/gemma4**: Could handle triage locally (free, fast, private)
- **codex CLI**: Could be used for specific repo types
- **gemini**: Additional option when available

This would be configured per-repo or per-action in `config.toml`.
