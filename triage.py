"""Triage engine — uses Claude to evaluate whether issues are ready for development."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import Config
from github_client import GithubClient, GithubClientError
from state import IssueRecord


@dataclass
class TriageDecision:
    decision: str       # "proceed" or "needs_info"
    confidence: str     # "high", "medium", "low"
    summary: str
    questions: list[str]


class TriageEngine:
    """Evaluates issues via Claude CLI to decide proceed vs. needs_info."""

    def __init__(self, config: Config, github: GithubClient) -> None:
        self._config = config
        self._github = github
        self._system_prompt = self._load_system_prompt()

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

        # Attempt 1
        decision = self._attempt_triage(user_prompt)
        if decision is not None:
            return decision

        # Attempt 2 (retry)
        decision = self._attempt_triage(user_prompt)
        if decision is not None:
            return decision

        # Conservative fallback
        return TriageDecision(
            decision="needs_info",
            confidence="low",
            summary="Triage failed after 2 attempts — defaulting to needs_info",
            questions=["Could you provide more details about the expected behavior?"],
        )

    def _attempt_triage(self, user_prompt: str) -> TriageDecision | None:
        """Single triage attempt. Returns None on failure."""
        try:
            raw_output = self._invoke_claude_triage(user_prompt)
            return self._parse_response(raw_output)
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, ValueError):
            return None

    def _build_prompt(self, record: IssueRecord, comments: list[dict]) -> str:
        """Build the user prompt with issue context."""
        parts = [
            f"Issue #{record.number} in {self._config.github.org}/{record.repo}: {record.title}",
            f"Action requested: {record.action} (from label ac-{record.action})",
            "",
            "Issue Body:",
            record.body or "(no body)",
        ]

        if comments:
            parts.append("")
            parts.append("Comments:")
            for c in comments:
                user = c.get("user", {}).get("login", "unknown")
                body = c.get("body", "")
                parts.append(f"  @{user}: {body}")

        return "\n".join(parts)

    def _invoke_claude_triage(self, user_prompt: str) -> str:
        """Call Claude CLI for triage and return raw stdout."""
        cmd = [
            "claude",
            "--print",
            "--output-format", "json",
            "--model", self._config.claude.triage_model,
            "--no-session-persistence",
            "--system-prompt", self._system_prompt,
            user_prompt,
        ]

        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=60,
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
        )

    def _extract_json_from_text(self, text: str) -> dict:
        """Extract JSON from text that may contain markdown fences or extra whitespace."""
        # Strip markdown code fences if present
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
        return json.loads(text)


def format_clarifying_comment(decision: TriageDecision, config: Config) -> str:
    """Format a needs_info triage decision into a GitHub comment."""
    lines = [
        "**auto-claude** needs more information before proceeding:\n",
        f"> {decision.summary}\n",
    ]

    if decision.questions:
        lines.append("**Questions:**\n")
        for q in decision.questions:
            lines.append(f"- {q}")

    lines.append("")
    lines.append("_Please respond to the questions above, then auto-claude will re-evaluate._")

    return "\n".join(lines)
