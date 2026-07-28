"""GitHub authentication for auto-claude's subprocesses.

auto-claude acts as a dedicated bot account (`accelevation-bot`) so that work it
does is attributable, and so its issues are invisible to the human `/loop`
runners - which scope themselves with `gh issue list --assignee @me`.

Two things make this less trivial than exporting a token:

1. The token is read from `AUTO_CLAUDE_GH_TOKEN` (or a gitignored `.gh_token`),
   NOT from `GH_TOKEN`. A `GH_TOKEN` exported in the operator's shell would also
   hijack their own interactive `gh` commands and their own loop. auto-claude
   translates its private variable into `GH_TOKEN` only inside the subprocess
   environments it builds.

2. `GH_TOKEN` governs `gh`, but NOT `git`. On Windows, `credential.helper=manager`
   is set at *system* scope and holds the human's credentials, so a plain
   `git push` authenticates as the human no matter what the token says. Network
   git commands must therefore be prefixed with `git_credential_args()`, which
   resets the inherited helper list and substitutes gh's credential helper.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# Deliberately not GH_TOKEN - see module docstring.
TOKEN_ENV_VAR = "AUTO_CLAUDE_GH_TOKEN"
TOKEN_FILENAME = ".gh_token"

# Runner signature used by preflight: (args) -> (returncode, stdout, stderr)
Runner = Callable[[Sequence[str]], "tuple[int, str, str]"]


def load_token(root: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """Load the bot token from the env var, else the gitignored token file.

    Returns None when unconfigured, in which case auto-claude falls back to the
    operator's own `gh` session rather than failing outright.
    """
    environ = os.environ if environ is None else environ

    from_env = _clean(environ.get(TOKEN_ENV_VAR) or "")
    if from_env:
        return from_env

    token_file = Path(root) / TOKEN_FILENAME
    try:
        # utf-8-sig, not utf-8: PowerShell's Set-Content / Out-File can prepend a
        # BOM, and Python's str.strip() does not treat U+FEFF as whitespace. The
        # corrupted token would only show up later as an opaque 401.
        content = token_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None

    for line in content.splitlines():
        line = _clean(line)
        if line and not line.startswith("#"):
            return line
    return None


def load_dotenv(root: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read `KEY=VALUE` lines from a gitignored `.env` beside config.toml.

    Deliberately minimal: no interpolation, no export keyword, no multi-line
    values. It exists so a secret like the metrics DB URL lives in one
    gitignored file instead of being pasted into a shell profile.

    An already-set environment variable always wins, so an operator can
    override any single value without editing the file.
    """
    environ = os.environ if environ is None else environ
    values: dict[str, str] = {}

    try:
        # utf-8-sig for the same reason as the token file: PowerShell writes BOMs.
        content = (Path(root) / ".env").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return values

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = _clean(key)
        if not key or key in environ:
            continue
        values[key] = _clean(value)

    return values


def _clean(value: str) -> str:
    """Strip whitespace, a stray BOM, and accidental surrounding quotes."""
    return value.strip().lstrip("﻿").strip().strip("'\"").strip()


def build_env(
    token: str | None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the environment for a `gh`/`git`/`claude` subprocess."""
    env = dict(os.environ if base is None else base)
    env["MSYS_NO_PATHCONV"] = "1"
    if token:
        env["GH_TOKEN"] = token
    return env


def git_credential_args(token: str | None) -> list[str]:
    """`git -c` flags forcing gh's credential helper for network operations.

    Returns [] when no bot token is configured, leaving git behaviour untouched.

    The empty `credential.helper=` resets the inherited helper list - without it
    the system-scope Git Credential Manager still wins and pushes are attributed
    to whoever's credentials it has cached. The token itself is never placed in
    argv (visible in process listings); it reaches gh through the environment.
    """
    if not token:
        return []
    return [
        "-c", "credential.helper=",
        "-c", "credential.helper=!gh auth git-credential",
    ]


def current_token() -> str | None:
    """The bot token for this process, if `main` loaded one at startup.

    Reads only the private variable, never GH_TOKEN. Worker processes inherit it
    because it is placed in `os.environ` before they are spawned.
    """
    return (os.environ.get(TOKEN_ENV_VAR) or "").strip() or None


# git subcommands that talk to the remote and therefore need credentials.
_NETWORK_GIT_SUBCOMMANDS = frozenset({
    "fetch", "pull", "push", "clone", "ls-remote", "remote", "submodule",
})

# git global flags that take a value, so the subcommand is one token further on.
_GIT_FLAGS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def git_subcommand(cmd: Sequence[str]) -> str | None:
    """Extract the git subcommand, skipping global flags like `-C dir` / `-c k=v`."""
    if not cmd or cmd[0] != "git":
        return None

    i = 1
    while i < len(cmd):
        token = cmd[i]
        if token in _GIT_FLAGS_WITH_VALUE:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return None


def apply_git_credentials(
    cmd: Sequence[str],
    token: str | None = None,
) -> list[str]:
    """Insert credential-helper flags into a git command that hits the network.

    Non-git commands, local-only git commands, and the unconfigured case are
    returned unchanged.
    """
    cmd = list(cmd)
    token = current_token() if token is None else token
    if not token:
        return cmd

    sub = git_subcommand(cmd)
    if sub not in _NETWORK_GIT_SUBCOMMANDS:
        return cmd

    return [cmd[0], *git_credential_args(token), *cmd[1:]]


@dataclass(frozen=True)
class Check:
    """One preflight result."""

    ok: bool
    label: str
    detail: str
    fatal: bool = True

    def format(self) -> str:
        mark = "OK  " if self.ok else ("FAIL" if self.fatal else "WARN")
        return f"[{mark}] {self.label}: {self.detail}"


def _make_runner(token: str | None) -> Runner:
    """Build a subprocess runner that executes `gh` AS the given token.

    The env is mandatory: without it `gh` falls back to the operator's stored
    credentials, and preflight cheerfully reports the wrong identity - which is
    indistinguishable from a genuinely bad token.
    """
    import subprocess

    env = build_env(token)

    def _run(args: Sequence[str]) -> tuple[int, str, str]:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace", env=env,
        )
        return (proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip())

    return _run


def check_ownership_config(token: str | None, bot_login: str | None) -> list[Check]:
    """The un-skippable gate: does auto-claude know who it is?

    Pure config coherence, no network - so it runs before (and independently of)
    the credential checks, and cannot be bypassed with --skip-preflight.

    Both failures let auto-claude run "rogue": without `bot_login` the poller
    sends no assignee filter and every labelled issue in the org is fair game,
    including ones a human /loop runner has already claimed; without a token,
    `gh` falls back to the operator's own credentials and the work is attributed
    to a person who did not do it.
    """
    checks: list[Check] = []

    if not (bot_login or "").strip():
        checks.append(Check(
            ok=False, label="ownership",
            detail=(
                "no `bot_login` in [github] - auto-claude would pick up any "
                "ac-* labelled issue in the org regardless of who it is "
                "assigned to, and collide with the human /loop runners. Set "
                "bot_login to the account issues are assigned to."
            ),
        ))
    else:
        checks.append(Check(
            ok=True, label="ownership",
            detail=f"scoped to issues assigned to '{bot_login}'",
        ))

    if not token:
        checks.append(Check(
            ok=False, label="bot token",
            detail=(
                f"no {TOKEN_ENV_VAR} or {TOKEN_FILENAME} found - every commit, "
                f"PR and comment would be attributed to whoever's credentials "
                f"the local gh session holds."
            ),
        ))
    else:
        checks.append(Check(ok=True, label="bot token", detail="loaded"))

    return checks


def verify_identity(
    token: str | None,
    expected_login: str | None = None,
    run: Runner | None = None,
) -> list[Check]:
    """Confirm the token actually authenticates as the expected account.

    Fatal regardless of whether a token was configured: running as the operator
    is precisely the outcome the bot account exists to prevent.
    """
    run = _make_runner(token) if run is None else run

    code, out, err = run(["gh", "api", "user", "--jq", ".login"])
    login = out.strip()

    if code != 0:
        return [Check(
            ok=False, label="identity",
            detail=f"`gh api user` failed - token invalid or unapproved ({err[:120]})",
        )]

    if expected_login and login != expected_login:
        return [Check(
            ok=False, label="identity",
            detail=(
                f"authenticated as '{login}', expected '{expected_login}'. "
                f"The token belongs to the wrong account - a PAT acts as whoever "
                f"created it, regardless of its resource owner."
            ),
        )]

    return [Check(ok=True, label="identity", detail=f"authenticated as '{login}'")]


def check_access(
    token: str | None,
    org: str,
    repos: Sequence[str],
    run: Runner | None = None,
) -> list[Check]:
    """Verify the token can actually do what auto-claude needs.

    Every failure names the specific missing grant, so an unattended run fails at
    boot with an actionable message instead of a 404 hours later.
    """
    run = _make_runner(token) if run is None else run
    checks: list[Check] = []

    # --- repo access --------------------------------------------------------
    for repo in repos:
        code, out, err = run(["gh", "api", f"repos/{org}/{repo}", "--jq", ".permissions"])
        if code != 0:
            if "404" in err or "Not Found" in err:
                detail = (
                    f"404 - usually means the token is awaiting org approval "
                    f"(Org Settings -> Personal access tokens -> Pending requests), "
                    f"not that the repo is missing"
                )
            else:
                detail = f"cannot read repo ({err[:120]})"
            checks.append(Check(ok=False, label=f"repo {repo}", detail=detail))
            continue

        try:
            perms = json.loads(out) if out else {}
        except json.JSONDecodeError:
            perms = {}

        if not perms.get("push"):
            checks.append(Check(
                ok=False, label=f"repo {repo}",
                detail="no push permission - grant Contents: Read and write",
            ))
        else:
            checks.append(Check(ok=True, label=f"repo {repo}", detail="read/write OK"))

    # --- projects scope (board sync) ---------------------------------------
    code, _out, err = run(["gh", "project", "list", "--owner", org, "--limit", "1"])
    if code != 0:
        checks.append(Check(
            ok=False, fatal=False, label="projects",
            detail=(
                f"cannot list org projects - board sync will silently no-op. "
                f"Grant Organization permissions -> Projects: Read and write, and add "
                f"the bot to the board itself ({err[:80]})"
            ),
        ))
    else:
        checks.append(Check(ok=True, label="projects", detail="board access OK"))

    return checks


def preflight(
    token: str | None,
    org: str,
    repos: Sequence[str],
    expected_login: str | None = None,
    run: Runner | None = None,
) -> list[Check]:
    """All startup checks, in the order a failure is most useful.

    `main` runs the gate (`check_ownership_config` + `verify_identity`) and
    `check_access` separately, because only the latter is skippable. This
    composition exists for callers that just want the full picture.
    """
    run = _make_runner(token) if run is None else run
    return (
        check_ownership_config(token, expected_login)
        + verify_identity(token, expected_login, run=run)
        + check_access(token, org, repos, run=run)
    )


def format_report(checks: Sequence[Check]) -> str:
    """Render preflight results as a block of text."""
    return "\n".join(c.format() for c in checks)


def has_fatal(checks: Sequence[Check]) -> bool:
    return any(c.fatal and not c.ok for c in checks)
