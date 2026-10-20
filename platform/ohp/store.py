"""SQLite-backed persistent store for tasks, events, projects and locks.

Design notes
------------
* One file, WAL journal, foreign keys on. The runtime workspace is ephemeral, so
  the database file must live somewhere that survives — ``OHP_DB`` points at it.
* Schema changes go through an ordered list of migrations keyed by
  ``PRAGMA user_version``; we never ``ALTER`` ad-hoc.
* Idempotency is enforced in the database, not in application code: the
  ``tasks.idempotency_key`` column is ``UNIQUE`` and :meth:`create_task` returns
  the *existing* task when the key collides.
* Concurrency is coordinated with a lease table (:meth:`claim_task`), not by
  assuming a single writer. A stale lease can be stolen after it expires.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import Event, Project, Task, TaskResult, TaskStatus, utcnow
from .state_machine import IllegalTransition, validate_transition

# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #

_MIGRATIONS: tuple[str, ...] = (
    # v1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS projects (
        name           TEXT PRIMARY KEY,
        repo           TEXT NOT NULL DEFAULT '',
        working_dir    TEXT NOT NULL DEFAULT '',
        default_branch TEXT NOT NULL DEFAULT 'main',
        stack          TEXT NOT NULL DEFAULT '[]',
        build_cmd      TEXT NOT NULL DEFAULT '',
        test_cmd       TEXT NOT NULL DEFAULT '',
        skills         TEXT NOT NULL DEFAULT '[]',
        mcp            TEXT NOT NULL DEFAULT '[]',
        status         TEXT NOT NULL DEFAULT 'active',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tasks (
        task_id          TEXT PRIMARY KEY,
        parent_task_id   TEXT REFERENCES tasks(task_id),
        idempotency_key  TEXT NOT NULL UNIQUE,
        title            TEXT NOT NULL,
        description      TEXT NOT NULL DEFAULT '',
        task_type        TEXT NOT NULL,
        priority         TEXT NOT NULL,
        complexity       TEXT NOT NULL,
        risk             TEXT NOT NULL,
        project          TEXT NOT NULL DEFAULT 'default',
        role             TEXT NOT NULL DEFAULT 'developer',
        working_dir      TEXT,
        acceptance       TEXT NOT NULL DEFAULT '[]',
        context          TEXT NOT NULL DEFAULT '',
        tools            TEXT NOT NULL DEFAULT '[]',
        skills           TEXT NOT NULL DEFAULT '[]',
        dependencies     TEXT NOT NULL DEFAULT '[]',
        requires_approval INTEGER NOT NULL DEFAULT 0,
        model            TEXT NOT NULL DEFAULT 'inherit',
        max_iterations   INTEGER NOT NULL DEFAULT 60,
        timeout_s        INTEGER NOT NULL DEFAULT 1800,
        max_attempts     INTEGER NOT NULL DEFAULT 3,
        status           TEXT NOT NULL,
        attempts         INTEGER NOT NULL DEFAULT 0,
        conversation_id  TEXT,
        result           TEXT,
        error            TEXT,
        blocked_reason   TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
    CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
    CREATE INDEX IF NOT EXISTS idx_tasks_parent  ON tasks(parent_task_id);

    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        task_id  TEXT NOT NULL,
        ts       TEXT NOT NULL,
        kind     TEXT NOT NULL,
        actor    TEXT NOT NULL,
        detail   TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, ts);

    CREATE TABLE IF NOT EXISTS locks (
        resource   TEXT PRIMARY KEY,
        owner      TEXT NOT NULL,
        acquired_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    """,
)


class Store:
    """Persistence facade. All public methods are safe to call concurrently."""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30.0, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    # -- lifecycle -------------------------------------------------------- #

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Run a block in an IMMEDIATE transaction, committing or rolling back."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for version, script in enumerate(_MIGRATIONS, start=1):
            if version <= current:
                continue
            # ``executescript`` manages its own transaction (it commits any
            # pending one first), so we must not wrap it in ``_tx``.
            self._conn.executescript(script)
            self._conn.execute(f"PRAGMA user_version={version}")

    # -- tasks ------------------------------------------------------------ #

    def create_task(self, task: Task) -> tuple[Task, bool]:
        """Insert ``task``, honouring idempotency.

        Returns ``(task, created)``. If a task with the same
        ``idempotency_key`` already exists, the stored task is returned with
        ``created=False`` and the caller must not spawn duplicate work.
        """
        existing = self.get_task_by_idempotency(task.idempotency_key)
        if existing is not None:
            return existing, False
        row = task.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        try:
            with self._tx() as conn:
                conn.execute(
                    f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", row
                )
        except sqlite3.IntegrityError:
            # Lost a race with a concurrent writer: return the winner.
            winner = self.get_task_by_idempotency(task.idempotency_key)
            if winner is None:
                raise
            return winner, False
        return task, True

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return _row_to_task(row) if row else None

    def get_task_by_idempotency(self, key: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        project: str | None = None,
        parent_task_id: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if parent_task_id is not None:
            clauses.append("parent_task_id = ?")
            params.append(parent_task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM tasks "
            f"{where} ORDER BY created_at ASC LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_task(r) for r in rows]

    def next_queued(self, project: str | None = None) -> Task | None:
        """Return the highest-priority queued task, oldest first within a rank."""
        params: list[Any] = []
        where = "WHERE status = 'queued'"
        if project:
            where += " AND project = ?"
            params.append(project)
        rows = self._conn.execute(
            f"SELECT * FROM tasks {where}", params
        ).fetchall()
        tasks = [_row_to_task(r) for r in rows]
        if not tasks:
            return None
        tasks.sort(key=lambda t: (t.priority.rank, t.created_at))
        return tasks[0]

    def set_status(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        actor: str = "dispatcher",
        detail: dict[str, Any] | None = None,
        error: str | None = None,
        blocked_reason: str | None = None,
        result: TaskResult | None = None,
        conversation_id: str | None = None,
        increment_attempts: bool = False,
    ) -> Task:
        """Validate and apply a state transition, then record an event.

        Raises :class:`~ohp.state_machine.IllegalTransition` if the edge is not
        in the transition table.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task {task_id}")
            task = _row_to_task(row)
            transition = validate_transition(task.status, target)
            if actor not in transition.actors:
                raise IllegalTransition(
                    f"actor '{actor}' may not perform {transition.trigger}"
                )
            updates: dict[str, Any] = {
                "status": target.value,
                "updated_at": utcnow(),
            }
            if error is not None:
                updates["error"] = error
            if blocked_reason is not None:
                updates["blocked_reason"] = blocked_reason
            if result is not None:
                updates["result"] = json.dumps(result.to_dict())
            if conversation_id is not None:
                updates["conversation_id"] = conversation_id
            if increment_attempts:
                updates["attempts"] = task.attempts + 1
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE task_id = :task_id",
                {**updates, "task_id": task_id},
            )
            self._append_event(
                conn,
                Event(
                    task_id=task_id,
                    kind=f"status:{target.value}",
                    actor=actor,
                    detail={"trigger": transition.trigger, **(detail or {})},
                ),
            )
        return self.get_task(task_id)  # type: ignore[return-value]

    def attach_conversation(self, task_id: str, conversation_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE tasks SET conversation_id = ?, updated_at = ? "
                "WHERE task_id = ?",
                (conversation_id, utcnow(), task_id),
            )
            self._append_event(
                conn,
                Event(
                    task_id=task_id,
                    kind="conversation_linked",
                    actor="dispatcher",
                    detail={"conversation_id": conversation_id},
                ),
            )

    # -- events ----------------------------------------------------------- #

    def append_event(self, event: Event) -> None:
        with self._tx() as conn:
            self._append_event(conn, event)

    def _append_event(self, conn: sqlite3.Connection, event: Event) -> None:
        conn.execute(
            "INSERT INTO events (event_id, task_id, ts, kind, actor, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.task_id,
                event.ts,
                event.kind,
                event.actor,
                json.dumps(event.detail, ensure_ascii=False),
            ),
        )

    def list_events(self, task_id: str, limit: int = 200) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? ORDER BY ts ASC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [
            Event(
                task_id=r["task_id"],
                kind=r["kind"],
                actor=r["actor"],
                detail=json.loads(r["detail"]),
                event_id=r["event_id"],
                ts=r["ts"],
            )
            for r in rows
        ]

    # -- locks / leases --------------------------------------------------- #

    def claim_task(
        self, task_id: str, owner: str, *, lease_seconds: int = 300
    ) -> bool:
        """Acquire an exclusive lease on ``task_id``.

        Returns ``True`` if the lease is now held by ``owner``. A stale lease
        (past ``expires_at``) is stolen. This is how two dispatchers avoid
        running the same task concurrently.
        """
        now = time.time()
        expires = now + lease_seconds
        resource = f"task:{task_id}"
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM locks WHERE resource = ?", (resource,)
            ).fetchone()
            if row is not None and row["expires_at"] > now and row["owner"] != owner:
                return False
            conn.execute(
                "INSERT INTO locks (resource, owner, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(resource) DO UPDATE SET "
                "owner=excluded.owner, acquired_at=excluded.acquired_at, "
                "expires_at=excluded.expires_at",
                (resource, owner, now, expires),
            )
        return True

    def release_task(self, task_id: str, owner: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM locks WHERE resource = ? AND owner = ?",
                (f"task:{task_id}", owner),
            )

    def sweep_expired_locks(self) -> int:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM locks WHERE expires_at < ?", (time.time(),))
        return cur.rowcount

    # -- projects --------------------------------------------------------- #

    def upsert_project(self, project: Project) -> Project:
        data = {
            **project.__dict__,
            "stack": json.dumps(project.stack),
            "skills": json.dumps(project.skills),
            "mcp": json.dumps(project.mcp),
            "updated_at": utcnow(),
        }
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO projects
                    (name, repo, working_dir, default_branch, stack, build_cmd,
                     test_cmd, skills, mcp, status, created_at, updated_at)
                VALUES
                    (:name, :repo, :working_dir, :default_branch, :stack, :build_cmd,
                     :test_cmd, :skills, :mcp, :status, :created_at, :updated_at)
                ON CONFLICT(name) DO UPDATE SET
                    repo=excluded.repo, working_dir=excluded.working_dir,
                    default_branch=excluded.default_branch, stack=excluded.stack,
                    build_cmd=excluded.build_cmd, test_cmd=excluded.test_cmd,
                    skills=excluded.skills, mcp=excluded.mcp, status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                data,
            )
        return project

    def get_project(self, name: str) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            return None
        return Project(
            name=row["name"],
            repo=row["repo"],
            working_dir=row["working_dir"],
            default_branch=row["default_branch"],
            stack=json.loads(row["stack"]),
            build_cmd=row["build_cmd"],
            test_cmd=row["test_cmd"],
            skills=json.loads(row["skills"]),
            mcp=json.loads(row["mcp"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_projects(self) -> list[Project]:
        names = [
            r["name"]
            for r in self._conn.execute(
                "SELECT name FROM projects ORDER BY name"
            ).fetchall()
        ]
        return [p for p in (self.get_project(n) for n in names) if p is not None]

    # -- maintenance ------------------------------------------------------ #

    def counts_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def stale_running_tasks(self, older_than_s: int = 3600) -> list[Task]:
        """Tasks stuck in a running-ish state past a heartbeat deadline."""
        from datetime import datetime, timezone

        cutoff = datetime.now(timezone.utc).timestamp() - older_than_s
        out: list[Task] = []
        for task in self.list_tasks(limit=10_000):
            if task.status not in (
                TaskStatus.RUNNING,
                TaskStatus.WAITING_FOR_AGENT,
            ):
                continue
            try:
                ts = datetime.fromisoformat(
                    task.updated_at.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                continue
            if ts < cutoff:
                out.append(task)
        return out


def _row_to_task(row: sqlite3.Row) -> Task:
    data: dict[str, Any] = dict(row)
    for field_name in ("acceptance", "tools", "skills", "dependencies"):
        data[field_name] = json.loads(data.get(field_name) or "[]")
    data["requires_approval"] = bool(data.get("requires_approval"))
    data["result"] = json.loads(data["result"]) if data.get("result") else None
    return Task.from_row(data)
