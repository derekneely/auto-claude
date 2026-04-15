Review all uncommitted changes, then create a well-structured commit.

## Steps
1. Run `git diff HEAD` and `git status` to understand all changes
2. If changes span unrelated concerns, split into separate atomic commits
3. Stage relevant files (prefer explicit `git add <file>` over `git add -A`)
4. Commit using conventional commit format (below)

## Commit Format
```
<type>(<scope>): <short summary in imperative mood> (ai-cc)

<body — explain WHY the change was made, not just what changed>
<reference the original task/prompt if relevant>
```

### Types
feat, fix, refactor, docs, style, test, chore, perf, ci

### Rules
- Summary line: imperative mood, lowercase, no period, under 72 chars
- Body: wrap at 80 chars, focus on motivation and context
- Scope: the module, component, or area affected (e.g., `auth`, `api`, `ui`)
- If multiple files changed, summarize the cohesive purpose, don't list files
- Always append `(ai-cc)` to the summary line to tag AI-generated commits
- Do NOT add Co-Authored-By lines or any other trailer attributing the commit to an AI model