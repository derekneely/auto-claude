"""GitHub client module that wraps the gh CLI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


class GithubClientError(Exception):
    """Custom exception for GitHub client errors."""

    def __init__(self, message: str, returncode: int = -1, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class GithubClient:
    """Wraps the gh CLI to interact with GitHub."""

    def __init__(self, org: str) -> None:
        self.org = org

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_gh(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        """Run a gh CLI command and return the CompletedProcess result.

        Raises GithubClientError on non-zero exit, timeout, or missing gh CLI.
        """
        cmd = ["gh"] + args
        # Prevent MSYS/Git Bash from converting /repos/... paths to filesystem paths
        env = os.environ.copy()
        env["MSYS_NO_PATHCONV"] = "1"
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise GithubClientError(
                f"gh command timed out after {timeout}s: {' '.join(cmd)}",
                returncode=-1,
                stderr="",
            )
        except FileNotFoundError:
            raise GithubClientError(
                "gh CLI not found. Please install the GitHub CLI (https://cli.github.com/).",
                returncode=-1,
                stderr="",
            )

        if result.returncode != 0:
            raise GithubClientError(
                f"gh command failed (exit {result.returncode}): {result.stderr.strip()}",
                returncode=result.returncode,
                stderr=result.stderr,
            )

        return result

    def _gh_api(
        self,
        endpoint: str,
        method: str = "GET",
        fields: dict | None = None,
        timeout: int = 30,
    ) -> dict | list:
        """Call the GitHub API via gh and return parsed JSON."""
        args = ["api", endpoint, "--method", method]
        if fields:
            for key, value in fields.items():
                args += ["--field", f"{key}={value}"]

        result = self._run_gh(args, timeout=timeout)

        if not result.stdout.strip():
            return {}

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GithubClientError(
                f"Failed to parse JSON response from gh api {endpoint}: {exc}",
                returncode=0,
                stderr="",
            )

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def list_issues(self, repo: str, state: str = "open") -> list[dict]:
        """Return open issues for a repo, excluding pull requests."""
        items = self._gh_api(f"/repos/{self.org}/{repo}/issues?state={state}&per_page=100")
        return [item for item in items if "pull_request" not in item]

    def get_issue(self, repo: str, number: int) -> dict:
        """Return a single issue by number."""
        return self._gh_api(f"/repos/{self.org}/{repo}/issues/{number}")

    def get_issue_comments(self, repo: str, number: int) -> list[dict]:
        """Return all comments for an issue."""
        return self._gh_api(f"/repos/{self.org}/{repo}/issues/{number}/comments")

    def post_comment(self, repo: str, number: int, body: str) -> None:
        """Post a comment on an issue."""
        self._run_gh(
            [
                "issue", "comment", str(number),
                "--repo", f"{self.org}/{repo}",
                "--body", body,
            ]
        )

    def add_label(self, repo: str, number: int, label: str) -> None:
        """Add a label to an issue."""
        self._run_gh(
            [
                "issue", "edit", str(number),
                "--repo", f"{self.org}/{repo}",
                "--add-label", label,
            ]
        )

    def remove_label(self, repo: str, number: int, label: str) -> None:
        """Remove a label from an issue."""
        self._run_gh(
            [
                "issue", "edit", str(number),
                "--repo", f"{self.org}/{repo}",
                "--remove-label", label,
            ]
        )

    def ensure_label_exists(
        self,
        repo: str,
        label: str,
        color: str = "c2e0c6",
        description: str = "",
    ) -> None:
        """Create a label if it doesn't already exist (422 means it already exists)."""
        try:
            self._gh_api(
                f"/repos/{self.org}/{repo}/labels",
                method="POST",
                fields={"name": label, "color": color, "description": description},
            )
        except GithubClientError as exc:
            # gh returns exit 1 with "HTTP 422" when the label already exists.
            if "422" in exc.stderr:
                return
            raise

    def create_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "dev",
    ) -> str:
        """Create a pull request and return its URL."""
        result = self._run_gh(
            [
                "pr", "create",
                "--repo", f"{self.org}/{repo}",
                "--title", title,
                "--body", body,
                "--head", head,
                "--base", base,
            ]
        )
        return result.stdout.strip()

    def clone_repo(self, repo: str, target_dir: Path) -> None:
        """Clone a repository to target_dir (longer timeout for large repos)."""
        self._run_gh(
            ["repo", "clone", f"{self.org}/{repo}", str(target_dir)],
            timeout=120,
        )
