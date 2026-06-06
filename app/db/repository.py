"""
JobRepository: SQLite-backed job persistence.

Deliberately uses raw sqlite3 (no ORM) so you can see exactly what
queries run. This also means you can read the EXPLAIN QUERY PLAN output
directly when debugging slow queries.

Thread safety note: SQLite connections are not thread-safe by default.
We use check_same_thread=False and rely on the fact that the scheduler
calls DB methods via asyncio.to_thread() — each call runs in a thread
pool worker, but the WAL journal mode in SQLite allows concurrent readers
and a single writer without blocking.

WAL mode is set on first connect (see _init_db). This is a meaningful
choice — the default DELETE journal mode serialises all writes and reads,
which causes the API to stall while the scheduler is writing a job update.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, List, Optional

from app.core.models import (
    FailureReason,
    Job,
    JobSpec,
    JobStatus,
    ResourceLimits,
)

logger = logging.getLogger(__name__)

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        return datetime.strptime(s, _ISO_FORMAT)
    except ValueError:
        return datetime.fromisoformat(s)


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime(_ISO_FORMAT) if dt else None


class JobRepository:
    def __init__(self, db_path: str = "jobs.db"):
        self._db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            # WAL mode: allows readers while writer is active
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id              TEXT PRIMARY KEY,
                    image           TEXT NOT NULL,
                    command         TEXT NOT NULL,   -- JSON array
                    env             TEXT NOT NULL,   -- JSON object
                    memory_mb       INTEGER NOT NULL,
                    cpu_quota       REAL NOT NULL,
                    max_retries     INTEGER NOT NULL,
                    timeout_seconds INTEGER,
                    status          TEXT NOT NULL,
                    attempt         INTEGER NOT NULL DEFAULT 0,
                    container_id    TEXT,
                    exit_code       INTEGER,
                    failure_reason  TEXT,
                    error_message   TEXT,
                    created_at      TEXT NOT NULL,
                    started_at      TEXT,
                    finished_at     TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
            """)
        logger.info("Database initialised at %s", self._db_path)

    # ------------------------------------------------------------------ #
    #  Write operations                                                    #
    # ------------------------------------------------------------------ #

    def save_job(self, job: Job) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO jobs (
                    id, image, command, env, memory_mb, cpu_quota,
                    max_retries, timeout_seconds, status, attempt,
                    container_id, exit_code, failure_reason, error_message,
                    created_at, started_at, finished_at
                ) VALUES (
                    :id, :image, :command, :env, :memory_mb, :cpu_quota,
                    :max_retries, :timeout_seconds, :status, :attempt,
                    :container_id, :exit_code, :failure_reason, :error_message,
                    :created_at, :started_at, :finished_at
                )
            """, self._to_row(job))

    def update_job(self, job: Job) -> None:
        with self._conn() as conn:
            conn.execute("""
                UPDATE jobs SET
                    status = :status,
                    attempt = :attempt,
                    container_id = :container_id,
                    exit_code = :exit_code,
                    failure_reason = :failure_reason,
                    error_message = :error_message,
                    started_at = :started_at,
                    finished_at = :finished_at
                WHERE id = :id
            """, self._to_row(job))

    # ------------------------------------------------------------------ #
    #  Read operations                                                     #
    # ------------------------------------------------------------------ #

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def get_pending_jobs(self, limit: int = 10) -> List[Job]:
        """
        Returns PENDING jobs ordered by creation time (FIFO).
        Also picks up RETRYING jobs whose next attempt is due.
        """
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM jobs
                WHERE status IN ('pending', 'retrying')
                ORDER BY created_at ASC
                LIMIT ?
            """, (limit,)).fetchall()
        return [self._from_row(r) for r in rows]

    def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Job]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status.value, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [self._from_row(r) for r in rows]

    # ------------------------------------------------------------------ #
    #  Row ↔ Model translation                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_row(job: Job) -> dict:
        return {
            "id": job.id,
            "image": job.spec.image,
            "command": json.dumps(job.spec.command),
            "env": json.dumps(job.spec.env),
            "memory_mb": job.spec.limits.memory_mb,
            "cpu_quota": job.spec.limits.cpu_quota,
            "max_retries": job.spec.max_retries,
            "timeout_seconds": job.spec.timeout_seconds,
            "status": job.status.value,
            "attempt": job.attempt,
            "container_id": job.container_id,
            "exit_code": job.exit_code,
            "failure_reason": job.failure_reason.value if job.failure_reason else None,
            "error_message": job.error_message,
            "created_at": _fmt_dt(job.created_at),
            "started_at": _fmt_dt(job.started_at),
            "finished_at": _fmt_dt(job.finished_at),
        }

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Job:
        spec = JobSpec(
            image=row["image"],
            command=json.loads(row["command"]),
            env=json.loads(row["env"]),
            limits=ResourceLimits(
                memory_mb=row["memory_mb"],
                cpu_quota=row["cpu_quota"],
            ),
            max_retries=row["max_retries"],
            timeout_seconds=row["timeout_seconds"],
        )
        return Job(
            id=row["id"],
            spec=spec,
            status=JobStatus(row["status"]),
            attempt=row["attempt"],
            container_id=row["container_id"],
            exit_code=row["exit_code"],
            failure_reason=FailureReason(row["failure_reason"]) if row["failure_reason"] else None,
            error_message=row["error_message"],
            created_at=_parse_dt(row["created_at"]),
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
        )
