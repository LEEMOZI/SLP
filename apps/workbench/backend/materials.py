import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import UploadFile
from pydantic import BaseModel

from .cases import CaseArchivedError, CaseRepository, utc_now
from .task_service import TaskCancelled, TaskExecutionContext
from .tasks import Task, TaskCaseNotFoundError, TaskOutcome, loads, new_id


SUPPORTED_FORMATS = {"pdf", "docx", "txt", "md"}


class Material(BaseModel):
    id: str
    case_id: str
    original_name: str
    format: str
    size_bytes: int
    sha256: str
    status: str
    current_version_id: Optional[str] = None
    latest_task_id: Optional[str] = None
    created_at: str
    updated_at: str


class ParseVersion(BaseModel):
    id: str
    material_id: str
    task_id: str
    attempt_id: str
    parser_version: str
    status: str
    page_count: Optional[int] = None
    segment_count: int
    char_count: int
    locator_type: Optional[str] = None
    coverage: Dict[str, Any]
    error: Optional[Dict[str, Any]] = None
    created_at: str
    finished_at: Optional[str] = None


class MaterialSegment(BaseModel):
    id: str
    parse_version_id: str
    sequence: int
    text: str
    status: str
    locator: Dict[str, Any]
    extraction_method: Optional[str] = None
    error: Optional[Dict[str, Any]] = None


class MaterialUploadResult(BaseModel):
    material: Material
    task: Task


class MaterialList(BaseModel):
    items: List[Material]
    total: int


class MaterialDetail(BaseModel):
    material: Material
    versions: List[ParseVersion]


class SegmentList(BaseModel):
    items: List[MaterialSegment]
    next_cursor: Optional[int] = None


class MaterialNotFoundError(Exception):
    pass


class MaterialIdempotencyConflictError(Exception):
    pass


class MaterialParseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DuplicateMaterialError(Exception):
    def __init__(self, duplicates: List[Material]) -> None:
        super().__init__("相同内容的材料已存在")
        self.duplicates = duplicates


class MaterialValidationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class MaterialLimits:
    max_file_bytes: int = 20 * 1024 * 1024
    max_files_per_case: int = 100

    @classmethod
    def from_env(cls) -> "MaterialLimits":
        return cls(
            max_file_bytes=_positive_env("SLP_CAPABILITY_MAX_FILE_BYTES", 20 * 1024 * 1024),
            max_files_per_case=_positive_env("SLP_CAPABILITY_MAX_FILES_PER_CASE", 100),
        )


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _json(value: Optional[Dict[str, Any]]) -> Optional[str]:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _model(model_class, row: sqlite3.Row):
    return model_class(**{key: row[key] for key in model_class.model_fields})


class MaterialRepository:
    def __init__(self, case_repository: CaseRepository):
        self.case_repository = case_repository

    def connect(self):
        return self.case_repository.connect()

    def require_case(self, case_id: str, writable: bool = False) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT archived_at FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
        if row is None:
            raise TaskCaseNotFoundError(case_id)
        if writable and row["archived_at"] is not None:
            raise CaseArchivedError(case_id)

    def create(
        self,
        *,
        material_id: str,
        case_id: str,
        original_name: str,
        file_format: str,
        size_bytes: int,
        sha256: str,
        storage_key: str,
        client_request_id: Optional[str],
        request_hash: Optional[str],
    ) -> Material:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO materials (
                    id, case_id, original_name, format, size_bytes, sha256,
                    storage_key, status, client_request_id, request_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    material_id,
                    case_id,
                    original_name,
                    file_format,
                    size_bytes,
                    sha256,
                    storage_key,
                    client_request_id,
                    request_hash,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM materials WHERE id = ?", (material_id,)).fetchone()
        return _model(Material, row)

    def get(self, material_id: str) -> Material:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM materials WHERE id = ?", (material_id,)).fetchone()
        if row is None:
            raise MaterialNotFoundError(material_id)
        return _model(Material, row)

    def get_storage_key(self, material_id: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT storage_key FROM materials WHERE id = ?", (material_id,)
            ).fetchone()
        if row is None:
            raise MaterialNotFoundError(material_id)
        return row["storage_key"]

    def list(self, case_id: str) -> List[Material]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM materials WHERE case_id = ? ORDER BY created_at DESC, id DESC",
                (case_id,),
            ).fetchall()
        return [_model(Material, row) for row in rows]

    def count(self, case_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM materials WHERE case_id = ?", (case_id,)
            ).fetchone()
        return row["count"]

    def find_idempotent(self, case_id: str, client_request_id: str):
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM materials
                WHERE case_id = ? AND client_request_id = ?
                """,
                (case_id, client_request_id),
            ).fetchone()
        return (_model(Material, row), row["request_hash"]) if row else None

    def duplicates(self, case_id: str, sha256: str) -> List[Material]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM materials WHERE case_id = ? AND sha256 = ?
                ORDER BY created_at, id
                """,
                (case_id, sha256),
            ).fetchall()
        return [_model(Material, row) for row in rows]

    def attach_task(self, material_id: str, task_id: str) -> Material:
        with self.connect() as connection:
            connection.execute(
                "UPDATE materials SET latest_task_id = ?, updated_at = ? WHERE id = ?",
                (task_id, utc_now(), material_id),
            )
            row = connection.execute("SELECT * FROM materials WHERE id = ?", (material_id,)).fetchone()
        return _model(Material, row)

    def create_version(self, material_id: str, task_id: str, attempt_id: str) -> ParseVersion:
        version_id = new_id()
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO material_parse_versions (
                    id, material_id, task_id, attempt_id, status, created_at
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (version_id, material_id, task_id, attempt_id, now),
            )
            connection.execute(
                "UPDATE materials SET status = 'running', updated_at = ? WHERE id = ?",
                (now, material_id),
            )
            row = connection.execute(
                "SELECT * FROM material_parse_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return self._version(row)

    @staticmethod
    def _version(row: sqlite3.Row) -> ParseVersion:
        values = {key: row[key] for key in ParseVersion.model_fields}
        values["coverage"] = loads(values["coverage"]) or {}
        values["error"] = loads(values["error"])
        return ParseVersion(**values)

    @staticmethod
    def _segment(row: sqlite3.Row) -> MaterialSegment:
        values = {key: row[key] for key in MaterialSegment.model_fields}
        values["locator"] = loads(values["locator"]) or {}
        values["error"] = loads(values["error"])
        return MaterialSegment(**values)

    def update_meta(self, version_id: str, event: Dict[str, Any]) -> None:
        coverage = event.get("coverage") or {}
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE material_parse_versions
                SET parser_version = ?, locator_type = ?, page_count = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    str(event.get("parser_version") or ""),
                    event.get("locator_type"),
                    coverage.get("total_pages"),
                    version_id,
                ),
            )

    def append_segment(self, version_id: str, event: Dict[str, Any]) -> Dict[str, int]:
        text = str(event.get("text") or "")
        locator = event.get("locator") or {}
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO material_segments (
                    id, parse_version_id, sequence, text, status, locator,
                    extraction_method, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    version_id,
                    int(event["index"]),
                    text,
                    str(event.get("status") or "ok"),
                    _json(locator),
                    event.get("extraction_method"),
                    _json(event.get("error")),
                ),
            )
            connection.execute(
                """
                UPDATE material_parse_versions
                SET segment_count = segment_count + 1, char_count = char_count + ?
                WHERE id = ? AND status = 'running'
                """,
                (len(text), version_id),
            )
            row = connection.execute(
                """
                SELECT segment_count, char_count FROM material_parse_versions WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
        return {"segment_count": row["segment_count"], "char_count": row["char_count"]}

    def finish_version(self, version_id: str, event: Dict[str, Any]) -> ParseVersion:
        status = str(event["status"])
        now = utc_now()
        coverage = event.get("coverage") or {}
        error = event.get("error")
        with self.connect() as connection:
            current = connection.execute(
                "SELECT material_id, char_count FROM material_parse_versions WHERE id = ?",
                (version_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE material_parse_versions
                SET status = ?, parser_version = ?, page_count = ?, coverage = ?,
                    error = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    str(event.get("parser_version") or ""),
                    coverage.get("total_pages"),
                    _json(coverage),
                    _json(error),
                    now,
                    version_id,
                ),
            )
            material_status = "partial" if status == "partial" and current["char_count"] else status
            current_version = version_id if current["char_count"] else None
            connection.execute(
                """
                UPDATE materials SET status = ?, current_version_id = COALESCE(?, current_version_id),
                    updated_at = ? WHERE id = ?
                """,
                (material_status, current_version, now, current["material_id"]),
            )
            row = connection.execute(
                "SELECT * FROM material_parse_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return self._version(row)

    def interrupt_version(
        self, version_id: str, status: str, code: str, message: str
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT material_id, char_count, status FROM material_parse_versions WHERE id = ?",
                (version_id,),
            ).fetchone()
            if current is None or current["status"] != "running":
                return
            connection.execute(
                """
                UPDATE material_parse_versions
                SET status = ?, error = ?, finished_at = ? WHERE id = ?
                """,
                (status, _json({"code": code, "message": message}), now, version_id),
            )
            material_status = "partial" if current["char_count"] else status
            current_version = version_id if current["char_count"] else None
            connection.execute(
                """
                UPDATE materials SET status = ?, current_version_id = COALESCE(?, current_version_id),
                    updated_at = ? WHERE id = ?
                """,
                (material_status, current_version, now, current["material_id"]),
            )

    def list_versions(self, material_id: str) -> List[ParseVersion]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM material_parse_versions
                WHERE material_id = ? ORDER BY created_at DESC, id DESC
                """,
                (material_id,),
            ).fetchall()
        return [self._version(row) for row in rows]

    def get_version(self, material_id: str, version_id: str) -> ParseVersion:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM material_parse_versions WHERE id = ? AND material_id = ?",
                (version_id, material_id),
            ).fetchone()
        if row is None:
            raise MaterialNotFoundError(version_id)
        return self._version(row)

    def list_segments(
        self, material_id: str, version_id: str, cursor: int = 0, limit: int = 100
    ) -> SegmentList:
        self.get_version(material_id, version_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM material_segments
                WHERE parse_version_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (version_id, cursor, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        items = [self._segment(row) for row in rows[:limit]]
        return SegmentList(
            items=items,
            next_cursor=items[-1].sequence if has_more and items else None,
        )

    def get_usable_version(self, case_id: str, version_id: str) -> ParseVersion:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT v.* FROM material_parse_versions v
                JOIN materials m ON m.id = v.material_id
                WHERE v.id = ? AND m.case_id = ?
                  AND v.status IN ('success', 'partial') AND v.char_count > 0
                """,
                (version_id, case_id),
            ).fetchone()
        if row is None:
            raise MaterialNotFoundError(version_id)
        return self._version(row)

    def get_search_segments(self, version_id: str) -> List[MaterialSegment]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM material_segments
                WHERE parse_version_id = ? AND status IN ('ok', 'partial') AND text <> ''
                ORDER BY sequence
                """,
                (version_id,),
            ).fetchall()
        return [self._segment(row) for row in rows]

    def recover_interrupted(self) -> None:
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT v.id, v.material_id, v.char_count FROM material_parse_versions v
                JOIN tasks t ON t.id = v.task_id AND t.active_attempt_id = v.attempt_id
                WHERE v.status = 'running' AND t.status = 'recovery_pending'
                """
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE material_parse_versions
                    SET status = 'recovery_pending', finished_at = ?, error = ? WHERE id = ?
                    """,
                    (
                        now,
                        _json({
                            "code": "recovery_pending",
                            "message": "服务重启，解析状态待恢复",
                        }),
                        row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE materials SET status = ?, current_version_id = CASE
                        WHEN ? THEN COALESCE(current_version_id, ?) ELSE current_version_id END,
                        updated_at = ? WHERE id = ?
                    """,
                    (
                        "partial" if row["char_count"] else "recovery_pending",
                        bool(row["char_count"]),
                        row["id"],
                        now,
                        row["material_id"],
                    ),
                )


class MaterialStorage:
    def __init__(self, data_dir: Path, limits: Optional[MaterialLimits] = None) -> None:
        self.files_root = (data_dir / "files").resolve()
        self.material_root = self.files_root / "cases"
        self.incoming = data_dir / "incoming"
        self.limits = limits or MaterialLimits.from_env()

    async def store(
        self,
        repository: MaterialRepository,
        case_id: str,
        upload: UploadFile,
        confirm_duplicate: bool,
        client_request_id: Optional[str],
    ) -> Tuple[Material, bool]:
        repository.require_case(case_id, writable=True)
        self.incoming.mkdir(parents=True, exist_ok=True)
        original_name = (upload.filename or "").replace("\\", "/").split("/")[-1].strip()
        if not original_name or len(original_name) > 255:
            raise MaterialValidationError("invalid_filename", "原文件名无效")
        file_format = Path(original_name).suffix.lower().lstrip(".")
        if file_format not in SUPPORTED_FORMATS:
            raise MaterialValidationError(
                "unsupported_format", "仅支持 PDF、DOCX、TXT、Markdown 文件", 415
            )

        incoming_path = self.incoming / f"{uuid.uuid4()}.upload"
        digest = hashlib.sha256()
        size = 0
        try:
            with incoming_path.open("xb") as stream:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.limits.max_file_bytes:
                        raise MaterialValidationError(
                            "file_too_large", "材料超过单文件大小限制", 413
                        )
                    digest.update(chunk)
                    stream.write(chunk)
            sha256 = digest.hexdigest()
            canonical = json.dumps(
                {"original_name": original_name, "format": file_format, "sha256": sha256},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            request_hash = hashlib.sha256(canonical).hexdigest()
            if client_request_id:
                existing = repository.find_idempotent(case_id, client_request_id)
                if existing is not None:
                    if existing[1] != request_hash:
                        raise MaterialIdempotencyConflictError(client_request_id)
                    return existing[0], False
            if repository.count(case_id) >= self.limits.max_files_per_case:
                raise MaterialValidationError(
                    "too_many_materials", "案件材料数量已达到上限", 409
                )
            duplicates = repository.duplicates(case_id, sha256)
            if duplicates and not confirm_duplicate:
                raise DuplicateMaterialError(duplicates)

            material_id = new_id()
            storage_key = (
                f"cases/{case_id}/materials/{material_id}/original.{file_format}"
            )
            final_path = self.files_root / storage_key
            final_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(incoming_path, final_path)
            except FileExistsError as error:
                raise MaterialValidationError(
                    "storage_conflict", "材料存储冲突，请重试", 409
                ) from error
            try:
                material = repository.create(
                    material_id=material_id,
                    case_id=case_id,
                    original_name=original_name,
                    file_format=file_format,
                    size_bytes=size,
                    sha256=sha256,
                    storage_key=storage_key,
                    client_request_id=client_request_id,
                    request_hash=request_hash if client_request_id else None,
                )
            except sqlite3.IntegrityError:
                final_path.unlink(missing_ok=True)
                if client_request_id:
                    existing = repository.find_idempotent(case_id, client_request_id)
                    if existing is not None:
                        if existing[1] != request_hash:
                            raise MaterialIdempotencyConflictError(client_request_id)
                        return existing[0], False
                raise
            return material, True
        finally:
            incoming_path.unlink(missing_ok=True)
            await upload.close()

    def original_path(self, repository: MaterialRepository, material_id: str) -> Path:
        storage_key = repository.get_storage_key(material_id)
        path = (self.files_root / storage_key).resolve(strict=True)
        path.relative_to(self.files_root)
        return path


class MaterialParseHandler:
    def __init__(
        self,
        repository: MaterialRepository,
        storage: MaterialStorage,
        gateway,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.gateway = gateway

    async def __call__(
        self, context: TaskExecutionContext, input_snapshot: Dict[str, Any]
    ) -> TaskOutcome:
        material_id = input_snapshot["material_id"]
        material = self.repository.get(material_id)
        version = self.repository.create_version(
            material_id, context.task_id, context.attempt_id
        )
        latest_result = {"parse_version_id": version.id, "segment_count": 0, "char_count": 0}
        try:
            await context.checkpoint()
            path = self.storage.original_path(self.repository, material_id)
            payload = {
                "material_id": material_id,
                "parse_version_id": version.id,
                "path": str(path),
                "sha256": material.sha256,
            }
            async for event in self.gateway.parse_events(payload):
                if (
                    event.get("material_id") != material_id
                    or event.get("parse_version_id") != version.id
                ):
                    raise MaterialParseError("invalid_event", "解析事件身份不一致")
                event_type = event.get("type")
                if event_type == "meta":
                    self.repository.update_meta(version.id, event)
                elif event_type == "segment":
                    latest_result = {
                        "parse_version_id": version.id,
                        **self.repository.append_segment(version.id, event),
                    }
                    await context.report_partial(
                        current_stage="正在解析材料",
                        progress_detail=f"已保存 {latest_result['segment_count']} 个解析段",
                        result=latest_result,
                    )
                elif event_type == "done":
                    finished = self.repository.finish_version(version.id, event)
                    result = {
                        "parse_version_id": version.id,
                        "segment_count": finished.segment_count,
                        "char_count": finished.char_count,
                    }
                    if finished.status == "failed":
                        error = finished.error or {
                            "code": "parse_failed",
                            "message": "材料解析失败",
                        }
                        raise MaterialParseError(error["code"], error["message"])
                    return TaskOutcome(
                        status="partial" if finished.status == "partial" else "completed",
                        current_stage="解析完成" if finished.status == "success" else "部分解析完成",
                        progress_detail=(
                            f"已保存 {finished.segment_count} 个解析段"
                            if finished.status == "success"
                            else "解析存在未覆盖范围，已保留可用内容"
                        ),
                        progress=100,
                        result=result,
                    )
                elif event_type == "error":
                    error = event.get("error") or {
                        "code": "parse_failed",
                        "message": "材料解析失败",
                    }
                    self.repository.interrupt_version(
                        version.id, "failed", error["code"], error["message"]
                    )
                    raise MaterialParseError(error["code"], error["message"])
            raise MaterialParseError("incomplete_stream", "解析服务未返回完成事件")
        except TaskCancelled:
            self.repository.interrupt_version(
                version.id, "cancelled", "cancelled", "任务已取消，保留已解析内容"
            )
            raise
        except MaterialParseError:
            self.repository.interrupt_version(
                version.id, "failed", "parse_failed", "材料解析失败"
            )
            raise
        except Exception:
            self.repository.interrupt_version(
                version.id, "failed", "parse_failed", "材料解析服务暂不可用或连接中断"
            )
            raise MaterialParseError("parse_failed", "材料解析服务暂不可用或连接中断") from None
