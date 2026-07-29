"""db/pool.py — a small, retrying Postgres connection wrapper.

`Database` owns exactly one lazily-opened connection, reused across calls,
and re-opened whenever it is found closed (dropped by the pooler, or by a
prior OperationalError). Autocommit: every write here is either a single
atomic statement (the lease claim) or safely re-runnable (ON CONFLICT
upserts), so there is nothing a transaction would buy that idempotency does
not already provide.
"""

from __future__ import annotations

import time
from typing import Callable

import psycopg


class DbUnavailable(RuntimeError):
    """Postgres could not be reached. Never raised for SQL/logic errors —
    those propagate as whatever psycopg/Postgres raised."""


class Database:
    def __init__(
        self,
        url: str,
        *,
        connect: Callable[..., "psycopg.Connection"] | None = None,
        connect_timeout: int = 10,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = url
        self._connect = connect or psycopg.connect
        self._connect_timeout = connect_timeout
        self._retries = retries
        self._sleep = sleep
        self._conn = None

    def _ensure_connected(self):
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = self._connect(
            self._url, connect_timeout=self._connect_timeout, autocommit=True
        )
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Autocommit. Returns [] for statements with no result set.
        Retries `retries` times on OperationalError, then raises DbUnavailable."""
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                conn = self._ensure_connected()
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    if cur.description is None:
                        return []
                    return cur.fetchall()
            except psycopg.OperationalError as exc:
                last_exc = exc
                # The connection is presumed dead — drop it so the next
                # attempt opens a fresh one instead of retrying a socket that
                # will never recover.
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                if attempt < self._retries:
                    self._sleep(2 ** attempt)
        raise DbUnavailable(
            f"could not reach Postgres after {self._retries + 1} attempt(s): {last_exc}"
        ) from last_exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
