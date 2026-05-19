"""Postgres-backed state store for vastlaunch jobs.

Requires DATABASE_URL environment variable (standard postgres:// connection string).
Call migrate() once at application startup to create tables.

Two insertion paths:
  enqueue()  — server path: job queued before an instance exists (config_yaml stored for replay)
  add()      — CLI path: job already has an instance_id at insert time

The poller only manages jobs where config_yaml IS NOT NULL (server-submitted).
CLI jobs are managed by the CLI process itself.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row


_UPDATABLE_FIELDS = frozenset({
    "instance_id", "host", "port", "status", "exit_code", "config_path", "logs",
    "workdir_key",
})

_TERMINAL_STATUSES = frozenset({"success", "failed", "stopped"})


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Point it at a Postgres instance, e.g. postgresql://user:pass@host/db"
        )
    return url


@contextmanager
def _conn():
    with psycopg.connect(_database_url(), row_factory=dict_row) as conn:
        yield conn


def migrate() -> None:
    """Create tables if they don't exist. Idempotent — safe to call on every startup."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id        TEXT PRIMARY KEY,
                instance_id   BIGINT UNIQUE,
                name          TEXT NOT NULL,
                config_yaml   TEXT,
                config_path   TEXT,
                workdir_key   TEXT,
                host          TEXT,
                port          INTEGER,
                status        TEXT NOT NULL DEFAULT 'queued',
                exit_code     INTEGER,
                logs          TEXT,
                started_at    DOUBLE PRECISION,
                updated_at    DOUBLE PRECISION
            )
        """)
        conn.execute("""
            ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workdir_key TEXT
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS offer_blacklist (
                offer_id  BIGINT PRIMARY KEY
            )
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# insertion
# ---------------------------------------------------------------------------

def enqueue(
    job_id: str,
    name: str,
    config_yaml: str,
    config_path: str | None = None,
) -> None:
    """Server path: insert a job in 'queued' state before an instance exists."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs (job_id, name, config_yaml, config_path, started_at, status)
            VALUES (%s, %s, %s, %s, %s, 'queued')
            """,
            (job_id, name, config_yaml, config_path, time.time()),
        )
        conn.commit()


def add(
    instance_id: int | str,
    *,
    job_id: str,
    name: str,
    config_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """CLI path: insert a job that already has an instance_id (status='launching')."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs
                (job_id, instance_id, name, config_path, host, port, started_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'launching')
            ON CONFLICT (job_id) DO NOTHING
            """,
            (job_id, int(instance_id), name, config_path, host, port, time.time()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------

def get(instance_id: int | str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE instance_id = %s",
            (int(instance_id),),
        ).fetchone()
    return dict(row) if row else None


def get_by_job_id(job_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def all_jobs() -> dict[str, dict]:
    """All jobs, keyed by instance_id (or job_id if instance not yet assigned). For CLI."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY started_at DESC"
        ).fetchall()
    return {
        str(row["instance_id"]) if row["instance_id"] else row["job_id"]: dict(row)
        for row in rows
    }


def all_active_jobs() -> list[dict]:
    """Server-submitted jobs not yet in a terminal state. Used by the poller."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status NOT IN ('success', 'failed', 'stopped')
              AND config_yaml IS NOT NULL
            ORDER BY started_at ASC
            """,
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# updates
# ---------------------------------------------------------------------------

def _do_update(where_col: str, where_val: Any, **fields: Any) -> None:
    invalid = set(fields) - _UPDATABLE_FIELDS
    if invalid:
        raise ValueError(f"unknown job fields: {invalid}")
    updates = {**fields, "updated_at": time.time()}
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [where_val]
    with _conn() as conn:
        conn.execute(
            f"UPDATE jobs SET {set_clause} WHERE {where_col} = %s",  # noqa: S608
            values,
        )
        conn.commit()


def update(instance_id: int | str, **fields: Any) -> None:
    _do_update("instance_id", int(instance_id), **fields)


def update_by_job_id(job_id: str, **fields: Any) -> None:
    _do_update("job_id", job_id, **fields)


def remove(instance_id: int | str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM jobs WHERE instance_id = %s", (int(instance_id),))
        conn.commit()


def remove_by_job_id(job_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# offer blacklist
# ---------------------------------------------------------------------------

def blacklist_get() -> list[int]:
    with _conn() as conn:
        rows = conn.execute("SELECT offer_id FROM offer_blacklist").fetchall()
    return [row["offer_id"] for row in rows]


def blacklist_add(offer_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO offer_blacklist (offer_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (offer_id,),
        )
        conn.commit()


def blacklist_clear() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM offer_blacklist")
        conn.commit()
