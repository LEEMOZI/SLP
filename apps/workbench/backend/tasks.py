import json
import sqlite3
import uuid
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from .cases import CaseArchivedError, CaseRepository, utc_now
from .migrations import run_migrations


TaskStatus = Literal[
    "queued",
    "running",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "recovery_pending",
]
ACTIVE_STATUSES = ("queued", "running")
RETRYABLE_STATUSES = ("failed", "partial", "cancelled", "recovery_pending")


class TaskAttempt(BaseModel):
    id: str
    task_id: str
    attempt_number: int
    status: TaskStatus
    current_stage: str
    progress: Optional[int] = None
    progress_detail: str
    input_snapshot: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    cancel_requested: bool
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class Task(BaseModel):
    id: str
    case_id: str
    case_name: str
    task_type: str
    title: str
    status: TaskStatus
    current_stage: str
    progress: Optional[int] = None
    progress_detail: str
    model_role: Optional[str] = None
    input_snapshot: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    attempt_number: int
    cancel_requested: bool
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class TaskDetail(Task):
    attempts: List[TaskAttempt]


class TaskList(BaseModel):
    items: List[Task]
    total: int
    demo_enabled: bool = False


class TaskRetry(BaseModel):
    scope: Optional[Dict[str, Any]] = None


class TaskOutcome(BaseModel):
    status: Literal["completed", "partial"] = "completed"
    current_stage: str
    progress_detail: str
    progress: Optional[int] = Field(default=None, ge=0, le=100)
    result: Optional[Dict[str, Any]] = None


class TaskNotFoundError(Exception):
    pass


class TaskCaseNotFoundError(Exception):
    pass


class TaskStateError(Exception):
    pass


class TaskIdempotencyConflictError(Exception):
    pass


def new_id() -> str:
    return str(uuid.uuid4())


def dumps(value: Optional[Dict[str, Any]]) -> Optional[str]:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def loads(value: Optional[str]) -> Optional[Dict[str, Any]]:
    return json.loads(value) if value is not None else None


def has_partial_result(serialized_result: Optional[str]) -> bool:
    if serialized_result is None:
        return False
    result = loads(serialized_result)
    return bool(result is not None and result.get("has_partial_results", True))


class TaskRepository:
    def __init__(self, case_repository: CaseRepository):
        self.case_repository = case_repository

    @property
    def db_path(self):
        return self.case_repository.db_path

    def connect(self):
        return self.case_repository.connect()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    task_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    progress INTEGER,
                    progress_detail TEXT NOT NULL,
                    model_role TEXT,
                    input_snapshot TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    active_attempt_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS task_attempts (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    progress INTEGER,
                    progress_detail TEXT NOT NULL,
                    input_snapshot TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE(task_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_case_created
                    ON tasks(case_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_task_attempts_task
                    ON task_attempts(task_id, attempt_number);
                """
            )
            run_migrations(connection)

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        values = dict(row)
        values["input_snapshot"] = loads(values["input_snapshot"])
        values["result"] = loads(values["result"])
        values["cancel_requested"] = bool(values["cancel_requested"])
        return Task(**values)

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> TaskAttempt:
        values = dict(row)
        values["input_snapshot"] = loads(values["input_snapshot"])
        values["result"] = loads(values["result"])
        values["cancel_requested"] = bool(values["cancel_requested"])
        return TaskAttempt(**values)

    @staticmethod
    def _task_query() -> str:
        return """
            SELECT t.*, c.name AS case_name, a.attempt_number, a.cancel_requested
            FROM tasks t
            JOIN cases c ON c.id = t.case_id
            JOIN task_attempts a ON a.id = t.active_attempt_id
        """

    def _get_in_connection(self, connection, task_id: str) -> Optional[Task]:
        row = connection.execute(
            self._task_query() + " WHERE t.id = ?", (task_id,)
        ).fetchone()
        return self._task_from_row(row) if row else None

    def get(self, task_id: str) -> Task:
        with self.connect() as connection:
            task = self._get_in_connection(connection, task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def get_detail(self, task_id: str) -> TaskDetail:
        with self.connect() as connection:
            task = self._get_in_connection(connection, task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            rows = connection.execute(
                "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY attempt_number",
                (task_id,),
            ).fetchall()
        return TaskDetail(
            **task.model_dump(),
            attempts=[self._attempt_from_row(row) for row in rows],
        )

    def list(self, case_id: Optional[str] = None) -> List[Task]:
        query = self._task_query()
        parameters = ()
        if case_id is not None:
            query += " WHERE t.case_id = ?"
            parameters = (case_id,)
        query += " ORDER BY t.created_at DESC, t.id DESC"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._task_from_row(row) for row in rows]

    def create(
        self,
        *,
        task_id: str,
        attempt_id: str,
        case_id: str,
        task_type: str,
        title: str,
        model_role: Optional[str],
        input_snapshot: Dict[str, Any],
        client_request_id: Optional[str] = None,
        request_hash: Optional[str] = None,
    ) -> Tuple[Task, bool]:
        now = utc_now()
        snapshot = dumps(input_snapshot)
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            case = connection.execute(
                "SELECT archived_at FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
            if case is None:
                raise TaskCaseNotFoundError(case_id)
            if case["archived_at"] is not None:
                raise CaseArchivedError(case_id)

            existing = None
            if client_request_id is not None:
                existing = connection.execute(
                    """
                    SELECT id, request_hash FROM tasks
                    WHERE case_id = ? AND task_type = ? AND client_request_id = ?
                    """,
                    (case_id, task_type, client_request_id),
                ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise TaskIdempotencyConflictError(client_request_id)
                return self._get_in_connection(connection, existing["id"]), False

            try:
                connection.execute(
                    """
                    INSERT INTO tasks (
                        id, case_id, task_type, title, status, current_stage,
                        progress_detail, model_role, input_snapshot, active_attempt_id,
                        client_request_id, request_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', '等待执行', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        case_id,
                        task_type,
                        title,
                        "任务已创建，等待执行",
                        model_role,
                        snapshot,
                        attempt_id,
                        client_request_id,
                        request_hash,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                if client_request_id is None:
                    raise
                existing = connection.execute(
                    """
                    SELECT id, request_hash FROM tasks
                    WHERE case_id = ? AND task_type = ? AND client_request_id = ?
                    """,
                    (case_id, task_type, client_request_id),
                ).fetchone()
                if existing is None:
                    raise
                if existing["request_hash"] != request_hash:
                    raise TaskIdempotencyConflictError(client_request_id)
                return self._get_in_connection(connection, existing["id"]), False

            connection.execute(
                """
                INSERT INTO task_attempts (
                    id, task_id, attempt_number, status, current_stage,
                    progress_detail, input_snapshot, created_at
                ) VALUES (?, ?, 1, 'queued', '等待执行', ?, ?, ?)
                """,
                (attempt_id, task_id, "任务已创建，等待执行", snapshot, now),
            )
            task = self._get_in_connection(connection, task_id)
        return task, True

    def find_by_client_request(
        self, case_id: str, task_type: str, client_request_id: str
    ) -> Optional[Task]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM tasks
                WHERE case_id = ? AND task_type = ? AND client_request_id = ?
                """,
                (case_id, task_type, client_request_id),
            ).fetchone()
            if row is None:
                return None
            return self._get_in_connection(connection, row["id"])

    def begin_attempt(self, task_id: str, attempt_id: str) -> Optional[Dict[str, Any]]:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT a.status, a.cancel_requested, a.input_snapshot
                FROM task_attempts a
                JOIN tasks t ON t.active_attempt_id = a.id
                WHERE t.id = ? AND a.id = ? AND t.status = 'queued'
                """,
                (task_id, attempt_id),
            ).fetchone()
            if row is None:
                return None
            if row["cancel_requested"]:
                detail = "任务已取消"
                connection.execute(
                    """
                    UPDATE task_attempts
                    SET status = 'cancelled', current_stage = '已取消',
                        progress_detail = ?, started_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (detail, now, now, attempt_id),
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'cancelled', current_stage = '已取消',
                        progress_detail = ?, updated_at = ?, started_at = ?, finished_at = ?
                    WHERE id = ? AND active_attempt_id = ?
                    """,
                    (detail, now, now, now, task_id, attempt_id),
                )
                return None
            connection.execute(
                """
                UPDATE task_attempts
                SET status = 'running', current_stage = '开始执行',
                    progress_detail = '执行器已启动', started_at = ?
                WHERE id = ?
                """,
                (now, attempt_id),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = 'running', current_stage = '开始执行',
                    progress_detail = '执行器已启动', updated_at = ?, started_at = ?,
                    finished_at = NULL
                WHERE id = ? AND active_attempt_id = ?
                """,
                (now, now, task_id, attempt_id),
            )
            return loads(row["input_snapshot"])

    def report(
        self,
        task_id: str,
        attempt_id: str,
        *,
        current_stage: str,
        progress_detail: str,
        progress: Optional[int],
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = utc_now()
        serialized_result = dumps(result)
        with self.connect() as connection:
            if serialized_result is None:
                connection.execute(
                    """
                    UPDATE task_attempts
                    SET current_stage = ?, progress_detail = ?, progress = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (current_stage, progress_detail, progress, attempt_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE task_attempts
                    SET current_stage = ?, progress_detail = ?, progress = ?, result = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (current_stage, progress_detail, progress, serialized_result, attempt_id),
                )
            connection.execute(
                """
                UPDATE tasks
                SET current_stage = ?, progress_detail = ?, progress = ?,
                    result = COALESCE(?, result), updated_at = ?
                WHERE id = ? AND active_attempt_id = ? AND status = 'running'
                """,
                (
                    current_stage,
                    progress_detail,
                    progress,
                    serialized_result,
                    now,
                    task_id,
                    attempt_id,
                ),
            )

    def is_cancellation_requested(self, task_id: str, attempt_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT a.cancel_requested
                FROM task_attempts a
                JOIN tasks t ON t.active_attempt_id = a.id
                WHERE a.id = ? AND t.id = ?
                """,
                (attempt_id, task_id),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def request_cancel(self, task_id: str) -> Task:
        now = utc_now()
        with self.connect() as connection:
            task = self._get_in_connection(connection, task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if task.status not in ACTIVE_STATUSES:
                raise TaskStateError("仅排队中或运行中的任务可以取消")
            if not task.cancel_requested:
                connection.execute(
                    """
                    UPDATE task_attempts
                    SET cancel_requested = 1
                    WHERE id = (SELECT active_attempt_id FROM tasks WHERE id = ?)
                    """,
                    (task_id,),
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET current_stage = '正在取消',
                        progress_detail = '已请求取消，将在当前步骤结束后停止', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, task_id),
                )
            task = self._get_in_connection(connection, task_id)
        return task

    def complete(self, task_id: str, attempt_id: str, outcome: TaskOutcome) -> None:
        now = utc_now()
        progress = 100 if outcome.status == "completed" else outcome.progress
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE task_attempts
                SET status = ?, current_stage = ?, progress_detail = ?, progress = ?,
                    result = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    outcome.status,
                    outcome.current_stage,
                    outcome.progress_detail,
                    progress,
                    dumps(outcome.result),
                    now,
                    attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, current_stage = ?, progress_detail = ?, progress = ?,
                    result = ?, error = NULL, updated_at = ?, finished_at = ?
                WHERE id = ? AND active_attempt_id = ? AND status = 'running'
                """,
                (
                    outcome.status,
                    outcome.current_stage,
                    outcome.progress_detail,
                    progress,
                    dumps(outcome.result),
                    now,
                    now,
                    task_id,
                    attempt_id,
                ),
            )

    def fail(self, task_id: str, attempt_id: str, error: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            attempt = connection.execute(
                "SELECT result FROM task_attempts WHERE id = ? AND status = 'running'",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                return
            partial_result = has_partial_result(attempt["result"])
            status = "partial" if partial_result else "failed"
            stage = "部分完成" if partial_result else "执行失败"
            detail = (
                "执行中断，已保留部分结果；可查看错误并重试"
                if partial_result
                else "执行失败，可查看错误并重试"
            )
            connection.execute(
                """
                UPDATE task_attempts
                SET status = ?, current_stage = ?, progress_detail = ?, progress = NULL,
                    error = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, stage, detail, error, now, attempt_id),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, current_stage = ?, progress_detail = ?, progress = NULL,
                    error = ?, updated_at = ?, finished_at = ?
                WHERE id = ? AND active_attempt_id = ? AND status = 'running'
                """,
                (status, stage, detail, error, now, now, task_id, attempt_id),
            )

    def finish_cancelled(self, task_id: str, attempt_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            attempt = connection.execute(
                "SELECT result FROM task_attempts WHERE id = ? AND status IN ('queued', 'running')",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                return
            partial_result = has_partial_result(attempt["result"])
            status = "partial" if partial_result else "cancelled"
            stage = "部分完成" if partial_result else "已取消"
            detail = (
                "任务已取消，已保留取消前产生的部分结果"
                if partial_result
                else "任务已取消，可基于输入快照重试"
            )
            connection.execute(
                """
                UPDATE task_attempts
                SET status = ?, current_stage = ?, progress_detail = ?,
                    error = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, stage, detail, detail, now, attempt_id),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, current_stage = ?, progress_detail = ?,
                    error = ?, updated_at = ?, finished_at = ?
                WHERE id = ? AND active_attempt_id = ?
                """,
                (status, stage, detail, detail, now, now, task_id, attempt_id),
            )

    def append_attempt(
        self,
        *,
        task_id: str,
        attempt_id: str,
        scope: Optional[Dict[str, Any]],
    ) -> Task:
        now = utc_now()
        with self.connect() as connection:
            task = self._get_in_connection(connection, task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            case = connection.execute(
                "SELECT archived_at FROM cases WHERE id = ?", (task.case_id,)
            ).fetchone()
            if case["archived_at"] is not None:
                raise CaseArchivedError(task.case_id)
            if task.status not in RETRYABLE_STATUSES:
                raise TaskStateError("仅失败、部分完成、已取消或状态待恢复的任务可以重试")
            input_snapshot = dict(task.input_snapshot)
            if scope is not None:
                input_snapshot["scope"] = scope
            snapshot = dumps(input_snapshot)
            attempt_number = task.attempt_number + 1
            connection.execute(
                """
                INSERT INTO task_attempts (
                    id, task_id, attempt_number, status, current_stage,
                    progress_detail, input_snapshot, created_at
                ) VALUES (?, ?, ?, 'queued', '等待执行', '重试已创建，等待执行', ?, ?)
                """,
                (attempt_id, task_id, attempt_number, snapshot, now),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = 'queued', current_stage = '等待执行',
                    progress = NULL, progress_detail = '重试已创建，等待执行',
                    input_snapshot = ?, result = NULL, error = NULL,
                    active_attempt_id = ?, updated_at = ?, started_at = NULL, finished_at = NULL
                WHERE id = ?
                """,
                (snapshot, attempt_id, now, task_id),
            )
            task = self._get_in_connection(connection, task_id)
        return task

    def recover_interrupted(self) -> int:
        now = utc_now()
        detail = "服务重启，无法确认上次执行结果；可基于输入快照重新执行"
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, active_attempt_id FROM tasks WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE task_attempts
                    SET status = 'recovery_pending', current_stage = '状态待恢复',
                        progress = NULL, progress_detail = ?, finished_at = ?
                    WHERE id = ? AND status IN ('queued', 'running')
                    """,
                    (detail, now, row["active_attempt_id"]),
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'recovery_pending', current_stage = '状态待恢复',
                        progress = NULL, progress_detail = ?, updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (detail, now, now, row["id"]),
                )
        return len(rows)
