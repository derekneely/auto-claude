# db/harness.py
"""db/harness.py — this process's identity row in auto_claude.harness.

Every lease and every run/summary row references a harness id, so `register`
succeeding — or, when Postgres is briefly unreachable, being journaled for
replay instead (see `main._register_harness` and `db/journal.py`) — comes
before anything else in db/ does useful work.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass

from db.pool import Database


@dataclass(frozen=True)
class Harness:
    id: str
    hostname: str
    pid: int
    version: str


def new_harness(version: str) -> Harness:
    return Harness(
        id=uuid.uuid4().hex,
        hostname=socket.gethostname(),
        pid=os.getpid(),
        version=version,
    )


def register(db: Database, harness: Harness) -> None:
    db.execute(
        """
        INSERT INTO auto_claude.harness (id, hostname, pid, version)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
           SET hostname = EXCLUDED.hostname,
               pid = EXCLUDED.pid,
               version = EXCLUDED.version,
               last_seen_at = now()
        """,
        (harness.id, harness.hostname, harness.pid, harness.version),
    )


def touch(db: Database, harness_id: str) -> None:
    db.execute(
        "UPDATE auto_claude.harness SET last_seen_at = now() WHERE id = %s",
        (harness_id,),
    )
