"""Triage engine — uses Claude to evaluate whether issues are ready for development."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import Config
from ghauth import build_env, current_token
from github_client import GithubClient, GithubClientError
from state import IssueRecord


# Read-only tool allowlist for the triage run. Everything not listed here is
# denied outright in --print mode, so triage can look but never touch: no
# writes, no `gh issue comment`, no `gh pr merge`. `gh api` is deliberately
# absent — `gh api -X POST` would slip a mutation past a prefix rule.
TRIAGE_TOOLS: tuple[str, ...] = (
    "Read",
    "Grep",
    "Glob",
    "Bash(gh issue view:*)",
    "Bash(gh issue list:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr list:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh search:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
)


def _first_json_object(text: str, required_key: str = "decision") -> dict:
    """The first balanced `{...}` in `text` that parses and has `required_key`.

    Brace-counting rather than a regex, because the object's own string values
    contain braces and escaped quotes. `required_key` is what stops a `{` in
    the model's prose or in a quoted code sample from being mistaken for the
    decision object.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                try:
                    candidate = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    candidate = None
                if isinstance(candidate, dict) and required_key in candidate:
                    return candidate
                start = -1

    raise json.JSONDecodeError(
        f"no JSON object containing {required_key!r} in model output", text, 0
    )


@dataclass
class TriageDecision:
    decision: str       # "proceed" or "needs_info"
    confidence: str     # "high", "medium", "low"
    summary: str
    questions: list[str]
    # What triage actually checked and concluded. Posted to the issue verbatim
    # on every decision — "go review X and report back" is a legitimate ask,
    # and without this field it had nowhere to land.
    findings: str = ""


class TriageEngine:
    """Evaluates issues via Claude CLI to decide proceed vs. needs_info.

    The run is an investigation, not a classification: it executes inside the
    repository checkout with read-only tools so it can resolve `#123`
    references, diff PRs, and read the code before deciding.
    """

    def __init__(self, config: Config, github: GithubClient, logger=None) -> None:
        self._config = config
        self._github = github
        self._logger = logger
        self._system_prompt = self._load_system_prompt()

    def _log(self, level: str, message: str) -> None:
        if self._logger is not None:
            getattr(self._logger, level)(message)

    def _load_system_prompt(self) -> str:
        prompt_file = self._config.paths.prompts_dir / "triage.txt"
        return prompt_file.read_text(encoding="utf-8")

    def triage(self, record: IssueRecord) -> TriageDecision:
        """Run triage on an issue. Retries once on failure, then falls back to needs_info."""
        comments = []
        try:
            comments = self._github.get_issue_comments(record.repo, record.number)
        except GithubClientError:
            pass  # Proceed with no comments — triage can still work

        user_prompt = self._build_prompt(record, comments)
        cwd = self._repo_cwd(record)
        claude = self._config.claude
        timeout = claude.triage_timeout_seconds

        # Attempt 2 escalates model and wall clock. Repeating attempt 1
        # verbatim only re-runs whatever shape of failure just happened.
        attempts = (
            (claude.triage_model, timeout),
            (claude.triage_escalation_model or claude.dev_model, timeout * 2),
        )

        for n, (model, attempt_timeout) in enumerate(attempts, start=1):
            decision = self._attempt_triage(user_prompt, cwd, model, attempt_timeout, n)
            if decision is not None:
                return decision

        # Conservative fallback. Deliberately does NOT invent a clarifying
        # question — triage errored out, it did not find a real gap, and
        # pretending otherwise is what makes the human do the agent's job.
        self._log("error", "Triage failed on both attempts — falling back to needs_info")
        return TriageDecision(
            decision="needs_info",
            confidence="low",
            summary=(
                "Triage could not complete — the investigation run failed twice. "
                "No conclusion was reached about this issue."
            ),
            questions=[
                "Triage errored out rather than finding a real gap. Re-apply the "
                "stage label to retry, or say how you'd like to proceed."
            ],
        )

    def _attempt_triage(
        self,
        user_prompt: str,
        cwd: Path,
        model: str,
        timeout: int,
        attempt: int,
    ) -> TriageDecision | None:
        """Single triage attempt. Returns None on failure."""
        raw_output = ""
        try:
            raw_output = self._invoke_claude_triage(user_prompt, cwd, model, timeout)
            return self._parse_response(raw_output)
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, ValueError) as exc:
            # Swallowing this bare is how a run that failed for a knowable
            # reason turned into a content-free question on the issue.
            self._log(
                "warn",
                f"Triage attempt {attempt} ({model}) failed: "
                f"{type(exc).__name__}: {exc}",
            )
            # An empty stdout and an unparseable reply raise the same
            # JSONDecodeError message ("line 1 column 1"), which sends anyone
            # reading the log after the wrong layer. Show what came back.
            if raw_output:
                from redact import redact
                self._log("warn", f"Triage raw reply: {redact(raw_output[:1200])}")
            return None

    def _repo_cwd(self, record: IssueRecord) -> Path:
        """The checkout triage investigates from.

        Without this the CLI ran in auto-claude's own directory, so every tool
        call landed in the wrong repository.
        """
        checkout = self._config.paths.repos_dir / record.repo
        if checkout.is_dir():
            return checkout
        self._log(
            "warn",
            f"No checkout at {checkout} — triaging {record.issue_id} without repo access",
        )
        repos_dir = self._config.paths.repos_dir
        return repos_dir if repos_dir.is_dir() else Path.cwd()

    def _build_prompt(self, record: IssueRecord, comments: list[dict]) -> str:
        """Build the user prompt with issue context.

        Comments posted after our own last comment are marked [NEW]: they are
        the human answering us, and an instruction in one of them is the task.
        Flattened undifferentiated, a "go look it up yourself" reply read as
        just more background and triage asked the same question again.
        """
        repo_slug = f"{self._config.github.org}/{record.repo}"
        parts = [
            f"Issue #{record.number} in {repo_slug}: {record.title}",
            f"Action requested: {record.action} (from label ac-{record.action})",
            f"Your working directory is a checkout of {repo_slug}. "
            f"Use --repo {repo_slug} on `gh` commands.",
            "",
            "Issue Body:",
            record.body or "(no body)",
        ]

        bot_login = self._config.github.bot_login
        last_bot = -1
        if bot_login:
            for i, c in enumerate(comments):
                if c.get("user", {}).get("login") == bot_login:
                    last_bot = i

        if comments:
            parts.append("")
            parts.append("Comments (oldest first):")
            for i, c in enumerate(comments):
                user = c.get("user", {}).get("login", "unknown")
                created = c.get("created_at", "")
                is_new = last_bot >= 0 and i > last_bot
                marker = "  [NEW — posted after your last comment]" if is_new else ""
                parts.append("")
                parts.append(f"--- @{user} at {created}{marker} ---")
                parts.append(c.get("body", ""))

        if last_bot >= 0 and last_bot < len(comments) - 1:
            parts.append("")
            parts.append(
                "The [NEW] comment(s) above are the human responding to your last "
                "request. Any instruction in them is your task: carry it out with "
                "your tools and report the result in `findings`. Do not re-ask "
                "anything they just told you to go find out yourself."
            )

        return "\n".join(parts)

    def _invoke_claude_triage(
        self, user_prompt: str, cwd: Path, model: str, timeout: int
    ) -> str:
        """Call Claude CLI for triage and return raw stdout.

        Runs in the repo checkout with a read-only tool allowlist, so triage
        can resolve references and read code instead of guessing from the
        issue text. Tools outside TRIAGE_TOOLS are denied in --print mode.
        """
        cmd = [
            "claude",
            "--print",
            "--output-format", "json",
            "--model", model,
            "--max-turns", str(self._config.claude.triage_max_turns),
            "--allowedTools", ",".join(TRIAGE_TOOLS),
            "--no-session-persistence",
            "--system-prompt", self._system_prompt,
            user_prompt,
        ]

        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd),
            env=build_env(current_token()),
            # Explicit: text=True alone decodes with the Windows locale codec
            # (cp1252), which dies on any non-ASCII byte in Claude's output.
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode != 0:
            raise subprocess.SubprocessError(
                f"Claude triage exited {result.returncode}: {result.stderr.strip()}"
            )

        return result.stdout

    def _parse_response(self, raw_output: str) -> TriageDecision:
        """Parse Claude's JSON output into a TriageDecision.

        Claude with --output-format json returns {"result": "...", ...}.
        The result field contains the actual response which should be our JSON.
        """
        # Parse the outer wrapper
        outer = json.loads(raw_output)

        # Extract the inner result — could be a string or already a dict
        inner = outer.get("result", outer)
        if isinstance(inner, str):
            inner = self._extract_json_from_text(inner)

        return TriageDecision(
            decision=inner["decision"],
            confidence=inner.get("confidence", "medium"),
            summary=inner.get("summary", ""),
            questions=inner.get("questions", []),
            findings=inner.get("findings", "") or "",
        )

    def _extract_json_from_text(self, text: str) -> dict:
        """Extract the decision object from the model's reply.

        The prompt asks for bare JSON and the model usually obliges, but not
        always — it opens with a sentence of narration and then fences the
        object. Anchored fence-stripping only ever handled a fence at
        character 0, so any preamble made the whole triage fail, twice, and
        post a content-free question to the issue.
        """
        text = text.strip()

        # Fast path: the whole reply is the object.
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

        return _first_json_object(text)


def format_clarifying_comment(decision: TriageDecision, config: Config) -> str:
    """Format a needs_info triage decision into a GitHub comment."""
    from redact import redact

    lines = [
        "**auto-claude** needs more information before proceeding:\n",
        f"> {decision.summary}\n",
    ]

    if decision.findings:
        lines.append("**What I checked:**\n")
        lines.append(decision.findings)
        lines.append("")

    if decision.questions:
        lines.append("**Questions:**\n")
        for q in decision.questions:
            lines.append(f"- {q}")

    lines.append("")
    lines.append("_Please respond to the questions above, then auto-claude will re-evaluate._")

    return redact("\n".join(lines))


def format_findings_comment(decision: TriageDecision, config: Config) -> str:
    """Report a `proceed` triage that actually investigated something.

    "Go look at X and tell me what you find" is a normal instruction, and
    before this the only way triage could say anything back was by refusing to
    proceed. A proceed with findings now reports and then gets on with it.
    """
    from redact import redact

    lines = [
        "**auto-claude** triage — proceeding:\n",
        f"> {decision.summary}\n",
    ]

    if decision.findings:
        lines.append("**Findings:**\n")
        lines.append(decision.findings)
        lines.append("")

    lines.append("_Queued for implementation._")

    return redact("\n".join(lines))


def format_stuck_comment(decision: TriageDecision, rounds: int) -> str:
    """Posted when triage has bounced one issue to ac-input-needed too often."""
    from redact import redact

    lines = [
        f"**auto-claude** has asked for clarification {rounds} times on this "
        "issue without reaching a decision. Marking `ac-blocked` and handing it "
        "to a human rather than asking again.\n",
        f"> {decision.summary}\n",
    ]

    if decision.findings:
        lines.append("**What I checked:**\n")
        lines.append(decision.findings)
        lines.append("")

    if decision.questions:
        lines.append("**Still open:**\n")
        for q in decision.questions:
            lines.append(f"- {q}")
        lines.append("")

    lines.append(
        "_Re-apply `ac-dev-ready` once the issue body is updated to unblock it._"
    )

    return redact("\n".join(lines))
