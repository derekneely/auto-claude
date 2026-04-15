"""Color-tagged logging system with multiprocessing queue support."""

import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing import Queue
from pathlib import Path

# 8 distinct ANSI colors for workers
COLOR_POOL: list[tuple[str, str]] = [
    ("RED",     "\033[91m"),
    ("GREEN",   "\033[92m"),
    ("YELLOW",  "\033[93m"),
    ("BLUE",    "\033[94m"),
    ("MAGENTA", "\033[95m"),
    ("CYAN",    "\033[96m"),
    ("ORANGE",  "\033[38;5;208m"),
    ("PURPLE",  "\033[38;5;129m"),
]

RESET = "\033[0m"
WHITE = "\033[97m"


def enable_ansi_windows() -> None:
    """Enable ANSI escape sequences and UTF-8 output on Windows."""
    if sys.platform != "win32":
        return
    os.system("")
    # Force UTF-8 stdout/stderr so Unicode characters don't crash on cp1252
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


@dataclass
class LogMessage:
    issue_id: str
    color_code: str
    color_name: str
    level: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    repo: str = ""


class ColorAssigner:
    """Assigns colors from COLOR_POOL to workers, recycling when released."""

    def __init__(self) -> None:
        self._available: list[tuple[str, str]] = list(COLOR_POOL)
        self._assigned: dict[str, tuple[str, str]] = {}

    def assign(self, issue_id: str) -> tuple[str, str]:
        """Assign a color to an issue. Returns (color_name, color_code)."""
        if issue_id in self._assigned:
            return self._assigned[issue_id]
        if not self._available:
            # Wrap around if we run out
            color = COLOR_POOL[len(self._assigned) % len(COLOR_POOL)]
        else:
            color = self._available.pop(0)
        self._assigned[issue_id] = color
        return color

    def release(self, issue_id: str) -> None:
        """Release a color back to the pool."""
        if issue_id in self._assigned:
            color = self._assigned.pop(issue_id)
            if color not in self._available:
                self._available.append(color)


class MainLogger:
    """Main process logger that drains the queue and writes to console + file."""

    def __init__(self, log_file: Path, colorize: bool = True, log_to_file: bool = True,
                 level: str = "INFO") -> None:
        self._log_file = log_file
        self._colorize = colorize
        self._log_to_file = log_to_file
        self._level = level
        self._file_handle = None

        if self._log_to_file:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = open(self._log_file, "a", encoding="utf-8")

    def info(self, message: str) -> None:
        self._log_main("INFO", message)

    def warn(self, message: str) -> None:
        self._log_main("WARN", message)

    def error(self, message: str) -> None:
        self._log_main("ERROR", message)

    def debug(self, message: str) -> None:
        if self._level == "DEBUG":
            self._log_main("DEBUG", message)

    def _log_main(self, level: str, message: str) -> None:
        """Log a message from the main process."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tag = "[auto-claude]"

        if self._colorize:
            print(f"{WHITE}{tag}{RESET}  {message}")
        else:
            print(f"{tag}  {message}")

        if self._file_handle:
            self._file_handle.write(f"{timestamp} {tag}  {message}\n")
            self._file_handle.flush()

    def drain_queue(self, log_queue: Queue) -> None:
        """Drain all pending messages from the worker log queue."""
        while not log_queue.empty():
            try:
                msg: LogMessage = log_queue.get_nowait()
            except Exception:
                break
            self._write_worker_message(msg)

    def _write_worker_message(self, msg: LogMessage) -> None:
        """Format and write a worker log message."""
        tag = f"[{msg.color_name:>8s} #{msg.issue_id.split('#')[-1]:<4s}]"
        repo = msg.repo or msg.issue_id.split("#")[0]

        if self._colorize:
            colored_tag = f"{msg.color_code}{tag}{RESET}"
            print(f"{colored_tag}  {repo:<32s}  {msg.message}")
        else:
            print(f"{tag}  {repo:<32s}  {msg.message}")

        if self._file_handle:
            self._file_handle.write(
                f"{msg.timestamp} {tag}  {repo:<32s}  {msg.message}\n"
            )
            self._file_handle.flush()

    def close(self) -> None:
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None


class WorkerLogger:
    """Logger for worker processes — sends messages via queue to main process."""

    def __init__(self, log_queue: Queue, issue_id: str, color_name: str,
                 color_code: str, repo: str = "") -> None:
        self._queue = log_queue
        self._issue_id = issue_id
        self._color_name = color_name
        self._color_code = color_code
        self._repo = repo

    def info(self, message: str) -> None:
        self._send("INFO", message)

    def warn(self, message: str) -> None:
        self._send("WARN", message)

    def error(self, message: str) -> None:
        self._send("ERROR", message)

    def _send(self, level: str, message: str) -> None:
        self._queue.put(LogMessage(
            issue_id=self._issue_id,
            color_code=self._color_code,
            color_name=self._color_name,
            level=level,
            message=message,
            repo=self._repo,
        ))
