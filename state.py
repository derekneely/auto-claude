"""
state.py — State management module for auto-claude.

Tracks issue lifecycle across all monitored repositories using a JSON-backed
flat-file store with atomic writes and enforced status transitions.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


# ---------------------------------------------------------------------------
# IssueStatus
# ---------------------------------------------------------------------------

class IssueStatus(str, Enum):
    DISCOVERED = "discovered"
    TRIAGING = "triaging"
    PLANNING = "planning"
    PLAN_POSTED = "plan_posted"
    NEEDS_INFO = "needs_info"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Valid state transitions
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[str, list[str]] = {
    IssueStatus.DISCOVERED:  [IssueStatus.TRIAGING, IssueStatus.SKIPPED],
    IssueStatus.TRIAGING:    [IssueStatus.QUEUED, IssueStatus.NEEDS_INFO, IssueStatus.PLANNING, IssueStatus.SKIPPED],
    IssueStatus.PLANNING:    [IssueStatus.PLAN_POSTED, IssueStatus.FAILED, IssueStatus.SKIPPED],
    IssueStatus.PLAN_POSTED: [IssueStatus.DISCOVERED, IssueStatus.SKIPPED],
    IssueStatus.NEEDS_INFO:  [IssueStatus.TRIAGING, IssueStatus.QUEUED, IssueStatus.SKIPPED],
    IssueStatus.QUEUED:      [IssueStatus.IN_PROGRESS, IssueStatus.SKIPPED],
    IssueStatus.IN_PROGRESS: [IssueStatus.COMPLETED, IssueStatus.FAILED, IssueStatus.INTERRUPTED, IssueStatus.SKIPPED],
    IssueStatus.FAILED:      [IssueStatus.QUEUED, IssueStatus.DISCOVERED, IssueStatus.SKIPPED],
    IssueStatus.INTERRUPTED: [IssueStatus.QUEUED, IssueStatus.DISCOVERED, IssueStatus.SKIPPED],
    IssueStatus.COMPLETED:   [IssueStatus.DISCOVERED, IssueStatus.QUEUED, IssueStatus.SKIPPED],
    IssueStatus.SKIPPED:     [],  # terminal
}


# ---------------------------------------------------------------------------
# InvalidTransitionError
# ---------------------------------------------------------------------------

class InvalidTransitionError(Exception):
    """Raised when a requested status transition is not permitted."""

    def __init__(self, from_status: str, to_status: str, message: str = "") -> None:
        self.from_status = from_status
        self.to_status = to_status
        self.message = message or (
            f"Cannot transition from '{from_status}' to '{to_status}'."
        )
        super().__init__(self.message)

    def __repr__(self) -> str:
        return (
            f"InvalidTransitionError(from_status={self.from_status!r}, "
            f"to_status={self.to_status!r}, message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# IssueRecord dataclass
# ---------------------------------------------------------------------------

@dataclass
class IssueRecord:
    issue_id: str           # "{repo}#{number}" — globally unique
    repo: str
    number: int
    title: str
    body: str
    labels: list[str]
    action: str             # "fix", "implement", "test", "plan", "review"
    status: str             # IssueStatus value
    discovered_at: str      # ISO datetime
    updated_at: str         # ISO datetime — last state change
    issue_updated_at: str   # GitHub's updated_at
    worker_pid: int | None = None
    branch: str | None = None
    pr_url: str | None = None
    triage_attempts: int = 0
    error: str | None = None
    rework_count: int = 0
    continuation_count: int = 0
    handoff_summary: str | None = None


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _record_to_dict(record: IssueRecord) -> dict:
    return asdict(record)


def _record_from_dict(data: dict) -> IssueRecord:
    return IssueRecord(**data)


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------

class StateStore:
    """JSON-backed flat-file store for IssueRecord objects."""

    def __init__(self, state_file: Path) -> None:
        self._state_file = Path(state_file)
        self._records: dict[str, IssueRecord] = {}  # issue_id -> IssueRecord
        self._load()

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Read the state file, populate _records. Handles missing / corrupt files."""
        if not self._state_file.exists():
            self._records = {}
            return

        try:
            raw = self._state_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            records = {}
            for item in data.get("issues", []):
                record = _record_from_dict(item)
                records[record.issue_id] = record
            self._records = records
        except (json.JSONDecodeError, KeyError, TypeError):
            # Back up the corrupted file and start fresh.
            backup_path = self._state_file.with_suffix(
                self._state_file.suffix + ".bak"
            )
            shutil.copy2(str(self._state_file), str(backup_path))
            self._records = {}

    def save(self) -> None:
        """Atomically write the current state to disk."""
        payload = {
            "issues": [_record_to_dict(r) for r in self._records.values()]
        }
        serialized = json.dumps(payload, indent=2, ensure_ascii=False)

        dir_path = self._state_file.parent
        dir_path.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialized)
            os.replace(tmp_path, str(self._state_file))
        except Exception:
            # Clean up the temp file if something went wrong.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, record: IssueRecord) -> None:
        """Add a new IssueRecord. Raises ValueError if issue_id already exists."""
        if record.issue_id in self._records:
            raise ValueError(
                f"Issue '{record.issue_id}' is already tracked. "
                "Use update() to modify existing records."
            )
        self._records[record.issue_id] = record

    def get(self, issue_id: str) -> IssueRecord | None:
        """Return the IssueRecord for the given issue_id, or None if not found."""
        return self._records.get(issue_id)

    def is_known(self, issue_id: str) -> bool:
        """Return True if the issue_id is already tracked."""
        return issue_id in self._records

    def update(self, issue_id: str, **kwargs) -> None:
        """Update arbitrary fields on an existing record and refresh updated_at."""
        record = self._records.get(issue_id)
        if record is None:
            raise KeyError(f"Issue '{issue_id}' not found in state store.")

        for key, value in kwargs.items():
            if not hasattr(record, key):
                raise AttributeError(
                    f"IssueRecord has no field '{key}'."
                )
            setattr(record, key, value)

        record.updated_at = _now_iso()

    def transition(self, issue_id: str, new_status: str) -> None:
        """
        Change the status of an issue, enforcing VALID_TRANSITIONS.

        Raises InvalidTransitionError if the transition is not permitted.
        Raises KeyError if the issue_id is not found.
        """
        record = self._records.get(issue_id)
        if record is None:
            raise KeyError(f"Issue '{issue_id}' not found in state store.")

        current_status = record.status
        allowed = VALID_TRANSITIONS.get(current_status, [])

        if new_status not in allowed:
            raise InvalidTransitionError(
                from_status=current_status,
                to_status=new_status,
                message=(
                    f"Cannot transition issue '{issue_id}' from '{current_status}' "
                    f"to '{new_status}'. Allowed next states: {allowed or ['(none — terminal)']}"
                ),
            )

        record.status = new_status
        record.updated_at = _now_iso()

    def get_by_status(self, status: str) -> list[IssueRecord]:
        """Return all IssueRecords with the given status."""
        return [r for r in self._records.values() if r.status == status]

    def all_records(self) -> list[IssueRecord]:
        """Return all tracked IssueRecords."""
        return list(self._records.values())
