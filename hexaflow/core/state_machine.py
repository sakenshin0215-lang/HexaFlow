import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from hexaflow.memory.local_db import LocalDB


RUNNING = "running"
FAILED = "failed"
SUSPENDED = "suspended"
COMPLETED = "completed"


@dataclass
class RunState:
    run_id: str
    trace_path: str
    task_name: str
    total_steps: int
    current_step_index: int
    status: str
    last_error: Optional[str]
    updated_at: str


@dataclass
class RunEvent:
    id: int
    run_id: str
    step_index: Optional[int]
    step_id: Optional[str]
    event: str
    detail: Optional[str]
    created_at: str


class StateMachine:
    """Persist and recover replay progress for checkpoint resume."""

    def __init__(self, db_path: str = "workspace/state/runtime.db"):
        self.db = LocalDB(db_path=db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    trace_path TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    total_steps INTEGER NOT NULL,
                    current_step_index INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step_index INTEGER,
                    step_id TEXT,
                    event TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
                """
            )

    def start_or_resume(self, trace_path: str, task_name: str, total_steps: int, resume: bool = True) -> RunState:
        now = datetime.utcnow().isoformat(timespec="seconds")
        if resume:
            state = self.get_latest_resumable(trace_path)
            if state:
                with self.db.connect() as conn:
                    conn.execute(
                        """
                        UPDATE runs
                        SET status = ?, updated_at = ?
                        WHERE run_id = ?
                        """,
                        (RUNNING, now, state.run_id),
                    )
                self._append_event(
                    state.run_id,
                    state.current_step_index,
                    None,
                    "run_resumed",
                    f"resume from step_index={state.current_step_index}",
                )
                return self.get_by_run_id(state.run_id)

        run_id = uuid.uuid4().hex[:12]
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, trace_path, task_name, total_steps, current_step_index, status, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, trace_path, task_name, total_steps, 0, RUNNING, None, now),
            )
            conn.execute(
                """
                INSERT INTO run_events (run_id, step_index, step_id, event, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, None, None, "run_started", "", now),
            )
        return self.get_by_run_id(run_id)

    def get_by_run_id(self, run_id: str) -> Optional[RunState]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._to_state(row) if row else None

    def get_latest_resumable(self, trace_path: str) -> Optional[RunState]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE trace_path = ? AND status IN (?, ?, ?)
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (trace_path, RUNNING, FAILED, SUSPENDED),
            ).fetchone()
        return self._to_state(row) if row else None

    def list_runs(self, status: Optional[str] = None, limit: int = 20) -> list[RunState]:
        with self.db.connect() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM runs
                    WHERE status = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM runs
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [self._to_state(r) for r in rows]

    def list_events(self, run_id: str, limit: int = 30) -> list[RunEvent]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM run_events
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        events = []
        for row in rows:
            events.append(
                RunEvent(
                    id=row["id"],
                    run_id=row["run_id"],
                    step_index=row["step_index"],
                    step_id=row["step_id"],
                    event=row["event"],
                    detail=row["detail"],
                    created_at=row["created_at"],
                )
            )
        return events

    def resume_by_run_id(self, run_id: str) -> RunState:
        now = datetime.utcnow().isoformat(timespec="seconds")
        old_state = self.get_by_run_id(run_id)
        if not old_state:
            raise ValueError(f"run_id 不存在: {run_id}")
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (RUNNING, now, run_id),
            )
        self._append_event(
            run_id,
            old_state.current_step_index,
            None,
            "run_resumed",
            f"manual resume from step_index={old_state.current_step_index}",
        )
        return self.get_by_run_id(run_id)

    def mark_step_started(self, run_id: str, step_index: int, step_id: str) -> None:
        self._append_event(run_id, step_index, step_id, "step_started", "")

    def mark_step_success(self, run_id: str, step_index: int, step_id: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        next_step = step_index + 1
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET current_step_index = ?, status = ?, last_error = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (next_step, RUNNING, now, run_id),
            )
        self._append_event(run_id, step_index, step_id, "step_success", "")

    def mark_step_skipped(self, run_id: str, step_index: int, step_id: str, reason: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        next_step = step_index + 1
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET current_step_index = ?, status = ?, last_error = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (next_step, RUNNING, now, run_id),
            )
        self._append_event(run_id, step_index, step_id, "step_skipped", reason)

    def mark_run_suspended(self, run_id: str, step_index: int, step_id: str, error: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, last_error = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (SUSPENDED, error[:1000], now, run_id),
            )
        self._append_event(run_id, step_index, step_id, "run_suspended", error)

    def mark_run_failed(self, run_id: str, step_index: int, step_id: str, error: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, last_error = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (FAILED, error[:1000], now, run_id),
            )
        self._append_event(run_id, step_index, step_id, "run_failed", error)

    def mark_run_completed(self, run_id: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, last_error = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (COMPLETED, now, run_id),
            )
        self._append_event(run_id, None, None, "run_completed", "")

    def add_event(
        self,
        run_id: str,
        event: str,
        detail: str = "",
        step_index: Optional[int] = None,
        step_id: Optional[str] = None,
    ) -> None:
        self._append_event(run_id, step_index, step_id, event, detail)

    def _append_event(self, run_id: str, step_index: Optional[int], step_id: Optional[str], event: str, detail: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO run_events (run_id, step_index, step_id, event, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, step_index, step_id, event, detail[:2000] if detail else "", now),
            )

    @staticmethod
    def _to_state(row) -> RunState:
        return RunState(
            run_id=row["run_id"],
            trace_path=row["trace_path"],
            task_name=row["task_name"],
            total_steps=row["total_steps"],
            current_step_index=row["current_step_index"],
            status=row["status"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )
