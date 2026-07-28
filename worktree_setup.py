"""Prepare a fresh git worktree so the repo's verify/test commands can run.

`git worktree add` gives you tracked source and nothing else. No `node_modules`,
no generated Prisma client, no gitignored env files. Every command in
`field_admin`'s `pipeline.json` needs all three, so without this the review
worker's checks fail on a missing toolchain:

    > nextn@0.1.0 typecheck
    > tsc --noEmit
    'tsc' is not recognized as an internal or external command

That is indistinguishable from a genuine failure at the review stage, so a good
PR would fail review three times and land at `ac-blocked` with feedback blaming
the code. The sibling toolchain avoids it because `test-pr-agent` does this
setup by hand; auto-claude is unattended and has to do it itself.

Setup commands are auto-detected from what is in the worktree, overridable
per-repo in auto-claude's own `config.toml`. Deliberately *not* a new key in
`.claude/pipeline.json`: that schema belongs to the sibling toolchain and
inventing keys in it risks a collision the day they add their own.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ghauth import build_env, current_token

#: Long enough for a cold `npm install` on a large lockfile, short enough that a
#: hung install does not pin a worker slot indefinitely.
DEFAULT_SETUP_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class RepoSetupConfig:
    """Per-repo `[repos.<name>]` block from config.toml. All parts optional.

    `setup=None` means "detect"; `setup=()` means "this repo genuinely needs
    nothing". The distinction matters — a block added only to name env files
    must not silently disable dependency installation.
    """

    setup: tuple[str, ...] | None = None
    env_files: tuple[str, ...] = ()
    env_source: Path | None = None
    #: source name -> name it lands under in the worktree. Exists so a *dev*
    #: env file can be landed as `.env.local`: `next build` sets
    #: NODE_ENV=production, so Next loads `.env.production` and ignores
    #: `.env.development` entirely, while `.env.local` loads in every mode.
    #: That gives the build a dev DATABASE_URL and keeps production
    #: credentials out of a worktree an agent runs in unattended.
    env_file_as: dict[str, str] = field(default_factory=dict)


@dataclass
class SetupResult:
    ok: bool = True
    transcript: str = ""
    copied_env: list[str] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)


def detect_setup_commands(worktree_dir: Path) -> tuple[str, ...]:
    """Infer the commands needed to make a checkout buildable.

    Covers the four repos in play. Gradle is deliberately absent: it resolves
    its own dependencies on first invocation, so `assembleDebug` needs no
    preparation.
    """
    commands: list[str] = []

    if (worktree_dir / "package.json").is_file():
        commands.append("npm install")

        # Sibling packages one level down. field_admin's root tsconfig includes
        # `**/*.ts` and excludes only `node_modules`, so it typechecks
        # `functions/src` — whose dependencies live in `functions/package.json`.
        # Without installing those, verify fails with "Cannot find module
        # firebase-functions/v2/firestore", which reads as a code error.
        # Bounded to one level: a full walk would find every fixture package.
        try:
            nested = sorted(
                p.name for p in worktree_dir.iterdir()
                if p.is_dir()
                and p.name != "node_modules"
                and (p / "package.json").is_file()
            )
        except OSError:
            nested = []
        commands.extend(f"npm --prefix {name} install" for name in nested)

        # Prisma is a devDependency, so the client can only be generated after
        # the install above.
        if (worktree_dir / "prisma" / "schema.prisma").is_file():
            commands.append("npx prisma generate")
        return tuple(commands)

    if (worktree_dir / "requirements.txt").is_file():
        return ("pip install -r requirements.txt",)
    if (worktree_dir / "pyproject.toml").is_file():
        return ("pip install -e .",)

    return ()


def resolve_setup_commands(
    worktree_dir: Path,
    config: RepoSetupConfig | None,
) -> tuple[str, ...]:
    """Configured commands if the repo declares any, else detection."""
    if config is not None and config.setup is not None:
        return tuple(config.setup)
    return detect_setup_commands(worktree_dir)


def copy_env_files(
    source_dir: Path | None,
    worktree_dir: Path,
    names: Sequence[str],
    rename: dict[str, str] | None = None,
) -> list[str]:
    """Copy gitignored env files into the worktree. Returns the names written.

    `rename` maps a source name to the name it lands under, so a dev env file
    can be placed as `.env.local` and picked up by a production-mode build.

    Never raises: a missing source directory or a missing file is the normal
    case for a repo that needs no env, and a build that genuinely needs one
    will fail loudly on its own terms rather than here.
    """
    if source_dir is None:
        return []
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        return []

    worktree_dir = Path(worktree_dir).resolve()
    copied: list[str] = []

    rename = rename or {}
    for name in names:
        target = rename.get(name, name)
        src = source_dir / name
        dst = worktree_dir / target
        # A configured name is not user input, but a traversal here would write
        # a secret outside the worktree — cheap to rule out.
        try:
            resolved = dst.resolve()
            if worktree_dir not in resolved.parents and resolved != worktree_dir:
                continue
        except OSError:
            continue
        if not src.is_file():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError:
            continue
        copied.append(target)

    return copied


def prepare_worktree(
    worktree_dir: Path,
    config: RepoSetupConfig | None,
    logger: Any = None,
    *,
    timeout: int = DEFAULT_SETUP_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> SetupResult:
    """Copy env files, then run the setup commands, in that order.

    A failing setup command is reported rather than raised: the caller decides
    whether that fails the worker (checks about to run) or is merely a warning.
    """
    result = SetupResult()

    if config is not None and config.env_files:
        result.copied_env = copy_env_files(
            config.env_source, worktree_dir, config.env_files, config.env_file_as
        )
        if result.copied_env and logger is not None:
            logger.info(f"Copied env files: {', '.join(result.copied_env)}")

    commands = resolve_setup_commands(worktree_dir, config)
    if not commands:
        return result

    run = runner or _default_runner
    sections: list[str] = []
    for command in commands:
        if logger is not None:
            logger.info(f"$ {command}")
        try:
            proc = run(command, cwd=worktree_dir, timeout=timeout)
        except subprocess.TimeoutExpired:
            result.ok = False
            sections.append(f"$ {command}\n[FAIL, timed out]")
            if logger is not None:
                logger.error(f"Setup command timed out: {command}")
            break
        result.ran.append(command)
        if proc.returncode != 0:
            result.ok = False
            tail = ((proc.stdout or "") + (proc.stderr or "")).strip()
            sections.append(
                f"$ {command}\n[FAIL, exit {proc.returncode}]"
                + (f"\n{tail[-2000:]}" if tail else "")
            )
            if logger is not None:
                logger.error(f"Setup command failed ({proc.returncode}): {command}")
            break

    result.transcript = "\n\n".join(sections)
    return result


def _default_runner(command: str, *, cwd: Path, timeout: int):
    """Shell out, matching the encoding/env discipline the rest of the repo uses."""
    return subprocess.run(
        command,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=build_env(current_token()),
    )
