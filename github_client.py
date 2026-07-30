"""GitHub client module that wraps the gh CLI."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

from ghauth import build_env, current_token


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
        # Also prevents MSYS/Git Bash converting /repos/... into filesystem paths
        env = build_env(current_token())
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=env,
                # Issue titles and bodies routinely contain smart quotes and
                # emoji. text=True alone decodes with cp1252 on Windows, which
                # kills the reader thread and leaves stdout as None.
                encoding="utf-8",
                errors="replace",
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

        # stdout is None when the reader thread died (historically: a decode
        # error). Fail with something diagnosable rather than AttributeError.
        if result.stdout is None:
            raise GithubClientError(
                f"gh api {endpoint} produced no readable output - the output "
                f"stream could not be decoded."
            )

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

    def list_issues(
        self,
        repo: str,
        state: str = "open",
        assignee: str | None = None,
    ) -> list[dict]:
        """Return issues for a repo, excluding pull requests.

        `assignee` scopes the query to one account - auto-claude's ownership
        boundary. Omitting it returns every issue regardless of owner, which is
        only correct when no bot account is configured.
        """
        query = f"state={state}&per_page=100"
        if assignee:
            query += f"&assignee={quote(assignee, safe='')}"
        items = self._gh_api(f"/repos/{self.org}/{repo}/issues?{query}")
        return [item for item in items if "pull_request" not in item]

    def get_file(self, repo: str, path: str, ref: str | None = None) -> str | None:
        """Return a file's contents from the repo, or None if it does not exist.

        Reads from GitHub rather than a local clone: auto-claude's clones are
        only refreshed when a worker runs, so a clone can be arbitrarily stale
        and report a file as missing long after it was added.
        """
        endpoint = f"/repos/{self.org}/{repo}/contents/{path}"
        if ref:
            endpoint += f"?ref={quote(ref, safe='')}"
        try:
            payload = self._gh_api(endpoint)
        except GithubClientError as exc:
            if "404" in exc.stderr or "Not Found" in exc.stderr:
                return None
            raise

        content = payload.get("content") if isinstance(payload, dict) else None
        if not content:
            return None
        return base64.b64decode(content).decode("utf-8", errors="replace")

    def get_issue(self, repo: str, number: int) -> dict:
        """Return a single issue by number."""
        return self._gh_api(f"/repos/{self.org}/{repo}/issues/{number}")

    def get_issue_comments(self, repo: str, number: int) -> list[dict]:
        """Return all comments for an issue."""
        return self._gh_api(f"/repos/{self.org}/{repo}/issues/{number}/comments")

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
    ) -> bool:
        """Create a label if absent. Returns True if it was created.

        Never updates an existing label: a 422 is success, not an error. The
        sibling toolchain stamps the same names with `--force`, and silently
        repainting its colours on every startup would be a nasty surprise.
        """
        try:
            self._gh_api(
                f"/repos/{self.org}/{repo}/labels",
                method="POST",
                fields={"name": label, "color": color, "description": description},
            )
            return True
        except GithubClientError as exc:
            # gh returns exit 1 with "HTTP 422" when the label already exists.
            if "422" in exc.stderr:
                return False
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

    def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict]:
        """Return all top-level reviews for a pull request."""
        return self._gh_api(f"/repos/{self.org}/{repo}/pulls/{pr_number}/reviews")

    def get_pr_review_comments(self, repo: str, pr_number: int) -> list[dict]:
        """Return all inline review comments on a pull request."""
        return self._gh_api(f"/repos/{self.org}/{repo}/pulls/{pr_number}/comments")

    def clone_repo(self, repo: str, target_dir: Path) -> None:
        """Clone a repository to target_dir (longer timeout for large repos)."""
        self._run_gh(
            ["repo", "clone", f"{self.org}/{repo}", str(target_dir)],
            timeout=120,
        )
