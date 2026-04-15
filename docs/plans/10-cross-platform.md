# Cross-Platform Support (Windows / Linux / macOS)

auto-claude must run on all three platforms. This doc covers every platform-specific concern and how we handle it.

## 1. multiprocessing: spawn vs fork

| Platform | Default start method |
|----------|---------------------|
| Windows | `spawn` (only option) |
| macOS (3.8+) | `spawn` (default since 3.8) |
| Linux | `fork` (default) |

**`spawn`** creates a fresh Python interpreter and imports the module — nothing is inherited from the parent except what's explicitly passed. This means:

- **Everything passed to a worker must be picklable.** Dataclasses, primitives, `multiprocessing.Queue`, and `multiprocessing.Event` are fine. Lambda functions, open file handles, loggers, and sockets are NOT.
- **`if __name__ == "__main__"` guard is required** in `main.py`, otherwise Windows will re-execute the module on every spawn.
- **Force `spawn` on all platforms** for consistency:
  ```python
  import multiprocessing
  multiprocessing.set_start_method("spawn")
  ```

### What this means for our code

- `IssueContext` dataclass: all fields must be simple types (str, int, float, Path, list, None). No objects with open handles. **Path objects are picklable** — this is fine.
- `WorkerLogger` is created *inside* the worker process, not passed from parent. We pass the raw `Queue`, `color_code`, `color_name` and let the worker construct its own logger.
- The worker function `run_worker()` / `run_plan_worker()` must be a top-level function (not a method on an unpicklable object, not a lambda).

## 2. Signal Handling

| Signal | Windows | Linux/macOS |
|--------|---------|-------------|
| SIGINT (Ctrl+C) | Works | Works |
| SIGTERM | Not raised by default; `signal.signal(SIGTERM, ...)` works but nothing sends it | Works (`kill <pid>`) |
| SIGBREAK | Windows-only (Ctrl+Break) | N/A |

### Our approach

```python
import signal
import sys

signal.signal(signal.SIGINT, handle_signal)

if sys.platform != "win32":
    signal.signal(signal.SIGTERM, handle_signal)
else:
    # On Windows, also catch SIGBREAK (Ctrl+Break in console)
    signal.signal(signal.SIGBREAK, handle_signal)
```

Worker process termination:
- `Process.terminate()` sends SIGTERM on Unix, calls `TerminateProcess()` on Windows. Both work — no code change needed.

## 3. ANSI Color Support

| Platform | Terminal | ANSI support |
|----------|----------|-------------|
| Windows 10+ | Windows Terminal, VS Code terminal | Yes (but must be enabled) |
| Windows 10+ | Legacy cmd.exe | Requires enabling via `SetConsoleMode` |
| Linux | All terminals | Yes |
| macOS | Terminal.app, iTerm2 | Yes |

### Our approach

Enable ANSI on Windows at startup in `logger.py`:

```python
import sys
import os

def enable_ansi_windows():
    """Enable ANSI escape sequences on Windows."""
    if sys.platform == "win32":
        os.system("")  # Simple trick that enables VT processing
        # More robust alternative:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # STD_OUTPUT_HANDLE = -11
            handle = kernel32.GetStdHandle(-11)
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass  # Fall back to no colors
```

Also expose a `colorize = false` config option as a manual override if colors don't work.

## 4. Atomic File Operations (State Persistence)

| Operation | Linux/macOS | Windows |
|-----------|-------------|---------|
| `os.rename(src, dst)` where dst exists | Atomic replace | **Fails** with `FileExistsError` |
| `os.replace(src, dst)` | Atomic replace | Atomic replace (Python 3.3+) |

### Our approach

Use `os.replace()` instead of `os.rename()` everywhere. It's atomic on all platforms:

```python
os.replace(tmp_path, str(self._path))  # Works on Windows, Linux, macOS
```

## 5. Path Handling

### Rules
- **Always use `pathlib.Path`** — never construct paths with string concatenation or `/`
- `Path` objects use the correct separator on each OS automatically
- When passing paths to `subprocess.run`, convert with `str(path)` — git/gh accept forward slashes on Windows too, but `str(Path)` uses `\` on Windows which is also fine
- **No hardcoded separators**: never write `f"{dir}/{file}"`, always write `dir / file` with Path

### Specific concerns
- Git worktree paths: `git worktree add` accepts both `/` and `\` on Windows
- `gh repo clone` target path: works with both separators
- Claude CLI `cwd`: `subprocess.Popen(cwd=str(path))` works cross-platform

## 6. Subprocess Execution

| Concern | Windows | Linux/macOS |
|---------|---------|-------------|
| `gh` binary | `gh.exe` on PATH | `gh` on PATH |
| `git` binary | `git.exe` on PATH | `git` on PATH |
| `claude` binary | `claude.cmd` or `claude.exe` | `claude` |
| Shell quoting | Different, but we don't use `shell=True` | N/A |

### Our approach

- **Never use `shell=True`** — pass command as a list, which avoids all quoting issues
- `subprocess.run(["gh", "api", ...])` works on all platforms (OS finds the executable)
- Always set `text=True` for consistent string output handling
- Use `timeout` parameter on all subprocess calls

## 7. File System Differences

| Concern | Windows | Linux/macOS |
|---------|---------|-------------|
| Max path length | 260 chars (default), can be extended | 4096 chars |
| Case sensitivity | Case-insensitive | Case-sensitive |
| Line endings | CRLF in some tools | LF |
| File locking | Mandatory locks possible | Advisory locks |

### Our approach
- Branch names and worktree paths should be kept short: `claude/issue-{number}-{short-title}` (max ~60 chars)
- Always open files with explicit `encoding="utf-8"` to avoid platform-dependent defaults
- Use `newline=""` or let git handle line endings (`.gitattributes`)
- JSON state file: `json.dump` with consistent formatting

## 8. Temp Files

```python
import tempfile
fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), ...)
```

This works on all platforms. The temp file is created in the same directory as the target to ensure `os.replace()` is atomic (same filesystem).

## 9. Process Management

| Concern | Windows | Linux/macOS |
|---------|---------|-------------|
| `Process.terminate()` | `TerminateProcess()` (immediate kill) | Sends SIGTERM (graceful) |
| `Process.kill()` | Same as terminate | Sends SIGKILL (immediate) |
| PID reuse | Possible | Possible |

### Our approach

On Windows, `terminate()` is a hard kill with no cleanup. To allow graceful shutdown:
- Workers check `abort_event.is_set()` between steps — this is the primary shutdown mechanism on all platforms
- `Process.terminate()` is only the fallback after the grace period expires
- This works identically on all platforms since we rely on the Event, not the signal

## 10. Summary: Code Patterns to Follow

```python
# Always
from pathlib import Path
import os

# Paths
path = base_dir / "subdir" / "file.json"    # not f"{base_dir}/subdir/file.json"

# Atomic replace
os.replace(src, dst)                          # not os.rename()

# Subprocess
subprocess.run(["git", "status"], text=True)  # not shell=True

# File I/O
open(path, "r", encoding="utf-8")            # always specify encoding

# multiprocessing
multiprocessing.set_start_method("spawn")     # force consistent behavior
if __name__ == "__main__": main()             # required guard

# Signals
if sys.platform != "win32":
    signal.signal(signal.SIGTERM, handler)
else:
    signal.signal(signal.SIGBREAK, handler)

# ANSI
enable_ansi_windows()                         # call at startup
```
