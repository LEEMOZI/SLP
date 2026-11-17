import json
import os
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .cases import CaseArchivedError, CaseRepository, utc_now
from .task_service import TaskExecutionContext
from .tasks import TaskCaseNotFoundError, TaskOutcome, TaskRepository, new_id


CITATION_PATTERN = re.compile(r"\[\[([^\[\]\s]{1,32})\]\]")
SUMMARY_PROVIDERS = ("builtin-extractive", "openai-compatible")


class SourceReference(BaseModel):
    kind: Literal["evidence", "material_segment"]
    id: str = Field(min_length=1, max_length=128)


class SummarySource(BaseModel):
    handle: str
    kind: Literal["evidence", "material_segment"]
    source_id: str
    case_id: str
    title: str
    text: str
    locator: Dict[str, Any] = Field(default_factory=dict)
    source_label: str = ""
    origin_status: str = ""
    attempt_id: Optional[str] = None
    task_id: Optional[str] = None
    material_id: Optional[str] = None
    parse_version_id: Optional[str] = None
    sequence: Optional[int] = None
    source_ref: Dict[str, Any] = Field(default_factory=dict)


class SummaryCreate(BaseModel):
    title: str = Field(default="研究摘要", min_length=1, max_length=160)
    question: str = Field(default="", max_length=2000)
    sources: List[SourceReference] = Field(default_factory=list, max_length=100)

    @field_validator("title", "question", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class SummarySourcesUpdate(BaseModel):
    add: List[SourceReference] = Field(default_factory=list, max_length=100)
    remove: List[str] = Field(default_factory=list, max_length=100)
    confirm_remove: bool = False


class SummaryVersionCreate(BaseModel):
    body: str = Field(default="", max_length=200000)
    base_version_id: Optional[str] = None


class CitationIssue(BaseModel):
    code: str
    message: str
    handle: Optional[str] = None


class AffectedCitation(BaseModel):
    handle: str
    version_id: str
    version_number: int


class SummaryVersion(BaseModel):
    id: str
    summary_id: str
    version_number: int
    status: Literal["draft", "edited", "confirmed"]
    body: str
    citations: Dict[str, SummarySource]
    base_version_id: Optional[str] = None
    task_id: Optional[str] = None
    attempt_id: Optional[str] = None
    generator_provider: Optional[str] = None
    requested_provider: Optional[str] = None
    actual_model: Optional[str] = None
    coverage: Dict[str, Any] = Field(default_factory=dict)
    saved_at: str
    confirmed_at: Optional[str] = None


class ResearchSummary(BaseModel):
    id: str
    case_id: str
    title: str
    question: str
    current_version_id: Optional[str] = None
    current_version_number: Optional[int] = None
    current_sources: List[SummarySource]
    next_source_number: int = Field(exclude=True)
    latest_task_id: Optional[str] = None
    created_at: str
    updated_at: str


class SummaryList(BaseModel):
    items: List[ResearchSummary]
    total: int


class SummaryDetail(BaseModel):
    summary: ResearchSummary
    sources: List[SummarySource]
    current_version: Optional[SummaryVersion] = None
    versions: List[SummaryVersion]
    coverage: Dict[str, Any] = Field(default_factory=dict)
    issues: List[CitationIssue] = Field(default_factory=list)


class SummaryNotFoundError(Exception):
    pass


class SummaryValidationError(Exception):
    pass


class SummaryConfirmationError(Exception):
    def __init__(self, issues: List[CitationIssue]) -> None:
        super().__init__("摘要仍存在引用或内容问题")
        self.issues = issues


class SummarySourceRemovalError(Exception):
    def __init__(self, affected: List[AffectedCitation]) -> None:
        super().__init__("移除来源会影响当前摘要引用")
        self.affected = affected


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Optional[str], default: Any) -> Any:
    return json.loads(value) if value is not None else default


def coverage_for_sources(sources: List[SummarySource]) -> Dict[str, Any]:
    partial = [
        {"handle": source.handle, "status": source.origin_status}
        for source in sources
        if source.origin_status not in {"", "completed", "success"}
    ]
    warnings = []
    if partial:
        warnings.append("部分来源来自部分完成的检索或材料解析，请核对覆盖范围")
    return {
        "source_count": len(sources),
        "evidence_count": sum(source.kind == "evidence" for source in sources),
        "material_segment_count": sum(
            source.kind == "material_segment" for source in sources
        ),
        "attempt_ids": sorted(
            {source.attempt_id for source in sources if source.attempt_id is not None}
        ),
        "material_version_ids": sorted(
            {
                source.parse_version_id
                for source in sources
                if source.parse_version_id is not None
            }
        ),
        "partial_sources": partial,
        "warnings": warnings,
    }


class SummaryRepository:
    def __init__(self, case_repository: CaseRepository) -> None:
        self.case_repository = case_repository

    def connect(self):
        return self.case_repository.connect()

    def require_case(self, case_id: str, writable: bool = False) -> None:
        with self.connect() as connection:
            self._require_case(connection, case_id, writable=writable)

    @staticmethod
    def _require_case(connection, case_id: str, writable: bool = False) -> None:
        row = connection.execute(
            "SELECT archived_at FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise TaskCaseNotFoundError(case_id)
        if writable and row["archived_at"] is not None:
            raise CaseArchivedError(case_id)

    @staticmethod
    def _summary_from_row(row) -> ResearchSummary:
        values = dict(row)
        values["current_sources"] = [
            SummarySource(**item)
            for item in _loads(values["current_sources"], [])
        ]
        return ResearchSummary(**values)

    @staticmethod
    def _version_from_row(row) -> SummaryVersion:
        values = dict(row)
        values["citations"] = {
            handle: SummarySource(**source)
            for handle, source in _loads(values["citations"], {}).items()
        }
        values["coverage"] = _loads(values["coverage"], {})
        return SummaryVersion(**values)

    def _get_in_connection(self, connection, summary_id: str) -> ResearchSummary:
        row = connection.execute(
            """
            SELECT s.*, v.version_number AS current_version_number
            FROM research_summaries s
            LEFT JOIN research_summary_versions v ON v.id = s.current_version_id
            WHERE s.id = ?
            """,
            (summary_id,),
        ).fetchone()
        if row is None:
            raise SummaryNotFoundError(summary_id)
        return self._summary_from_row(row)

    def get(self, summary_id: str) -> ResearchSummary:
        with self.connect() as connection:
            return self._get_in_connection(connection, summary_id)

    def list(self, case_id: str) -> List[ResearchSummary]:
        with self.connect() as connection:
            self._require_case(connection, case_id)
            rows = connection.execute(
                """
                SELECT s.*, v.version_number AS current_version_number
                FROM research_summaries s
                LEFT JOIN research_summary_versions v ON v.id = s.current_version_id
                WHERE s.case_id = ?
                ORDER BY s.updated_at DESC, s.id DESC
                """,
                (case_id,),
            ).fetchall()
        return [self._summary_from_row(row) for row in rows]

    def _resolve_source(
        self, connection, case_id: str, reference: SourceReference, handle: str
    ) -> SummarySource:
        if reference.kind == "evidence":
            row = connection.execute(
                """
                SELECT e.*, a.status AS origin_status
                FROM evidence_items e
                JOIN task_attempts a ON a.id = e.attempt_id
                WHERE e.id = ? AND e.case_id = ?
                """,
                (reference.id, case_id),
            ).fetchone()
            if row is None:
                raise SummaryValidationError("证据不存在、不可用或不属于当前案件")
            text = str(row["content"] or row["excerpt"] or "").strip()
            if not text:
                raise SummaryValidationError("所选证据没有可用于研究的文本")
            return SummarySource(
                handle=handle,
                kind="evidence",
                source_id=row["id"],
                case_id=case_id,
                title=row["title"],
                text=text,
                locator=_loads(row["locator"], {}),
                source_label=row["source_label"],
                origin_status=row["origin_status"],
                attempt_id=row["attempt_id"],
                task_id=row["task_id"],
                source_ref=_loads(row["source_ref"], {}),
            )
        row = connection.execute(
            """
            SELECT seg.*, m.id AS material_id, m.original_name, v.status AS origin_status
            FROM material_segments seg
            JOIN material_parse_versions v ON v.id = seg.parse_version_id
            JOIN materials m ON m.id = v.material_id
            WHERE seg.id = ? AND m.case_id = ?
              AND v.status IN ('success', 'partial')
              AND seg.status IN ('ok', 'partial')
            """,
            (reference.id, case_id),
        ).fetchone()
        if row is None:
            raise SummaryValidationError("材料段落不存在、已失效或不属于当前案件")
        if not str(row["text"] or "").strip():
            raise SummaryValidationError("所选材料段落没有可用于研究的文本")
        return SummarySource(
            handle=handle,
            kind="material_segment",
            source_id=row["id"],
            case_id=case_id,
            title=row["original_name"],
            text=row["text"],
            locator=_loads(row["locator"], {}),
            source_label="案件材料",
            origin_status=row["origin_status"],
            material_id=row["material_id"],
            parse_version_id=row["parse_version_id"],
            sequence=row["sequence"],
        )

    def _resolve_additions(
        self,
        connection,
        case_id: str,
        current: List[SummarySource],
        additions: List[SourceReference],
        next_number: int,
    ) -> tuple:
        sources = list(current)
        selected = {(source.kind, source.source_id) for source in sources}
        for reference in additions:
            key = (reference.kind, reference.id)
            if key in selected:
                continue
            source = self._resolve_source(
                connection, case_id, reference, f"S{next_number}"
            )
            next_number += 1
            sources.append(source)
            selected.add(key)
        return sources, next_number

    def create(self, case_id: str, payload: SummaryCreate) -> ResearchSummary:
        summary_id = new_id()
        now = utc_now()
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self._require_case(connection, case_id, writable=True)
            sources, _ = self._resolve_additions(
                connection, case_id, [], payload.sources, 1
            )
            connection.execute(
                """
                INSERT INTO research_summaries (
                    id, case_id, title, question, current_sources,
                    next_source_number, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_id,
                    case_id,
                    payload.title,
                    payload.question,
                    _dumps([source.model_dump(mode="json") for source in sources]),
                    len(sources) + 1,
                    now,
                    now,
                ),
            )
            return self._get_in_connection(connection, summary_id)

    def _versions(self, connection, summary_id: str) -> List[SummaryVersion]:
        rows = connection.execute(
            """
            SELECT * FROM research_summary_versions
            WHERE summary_id = ? ORDER BY version_number DESC
            """,
            (summary_id,),
        ).fetchall()
        return [self._version_from_row(row) for row in rows]

    def validate_version(
        self,
        summary: ResearchSummary,
        version: Optional[SummaryVersion],
    ) -> List[CitationIssue]:
        if version is None:
            return [CitationIssue(code="empty_summary", message="摘要正文不能为空")]
        issues: List[CitationIssue] = []
        if not version.body.strip():
            issues.append(CitationIssue(code="empty_summary", message="摘要正文不能为空"))
        selected = {source.handle: source for source in summary.current_sources}
        for handle in dict.fromkeys(CITATION_PATTERN.findall(version.body)):
            source = version.citations.get(handle)
            if source is None:
                issues.append(
                    CitationIssue(
                        code="unknown_citation",
                        message=f"引用 {handle} 没有对应的来源快照",
                        handle=handle,
                    )
                )
                continue
            if source.case_id != summary.case_id:
                issues.append(
                    CitationIssue(
                        code="cross_case_citation",
                        message=f"引用 {handle} 不属于当前案件",
                        handle=handle,
                    )
                )
                continue
            current = selected.get(handle)
            if current is None:
                issues.append(
                    CitationIssue(
                        code="unselected_citation",
                        message=f"引用 {handle} 的来源已从当前研究选择中移除",
                        handle=handle,
                    )
                )
            elif (
                current.kind != source.kind
                or current.source_id != source.source_id
                or not source.text.strip()
            ):
                issues.append(
                    CitationIssue(
                        code="invalid_citation",
                        message=f"引用 {handle} 的来源已失效",
                        handle=handle,
                    )
                )
        return issues

    def detail(self, summary_id: str) -> SummaryDetail:
        with self.connect() as connection:
            summary = self._get_in_connection(connection, summary_id)
            versions = self._versions(connection, summary_id)
        current = next(
            (version for version in versions if version.id == summary.current_version_id),
            None,
        )
        coverage = (
            current.coverage
            if current is not None and current.coverage
            else coverage_for_sources(summary.current_sources)
        )
        return SummaryDetail(
            summary=summary,
            sources=summary.current_sources,
            current_version=current,
            versions=versions,
            coverage=coverage,
            issues=self.validate_version(summary, current),
        )

    def update_sources(
        self, summary_id: str, payload: SummarySourcesUpdate
    ) -> SummaryDetail:
        with self.connect() as connection:
            summary = self._get_in_connection(connection, summary_id)
            self._require_case(connection, summary.case_id, writable=True)
            sources, next_number = self._resolve_additions(
                connection,
                summary.case_id,
                summary.current_sources,
                payload.add,
                summary.next_source_number,
            )
            remove = set(payload.remove)
            known = {source.handle for source in sources}
            if remove - known:
                raise SummaryValidationError("要移除的来源句柄不存在")
            current = None
            if summary.current_version_id is not None:
                row = connection.execute(
                    "SELECT * FROM research_summary_versions WHERE id = ?",
                    (summary.current_version_id,),
                ).fetchone()
                current = self._version_from_row(row) if row is not None else None
            affected: List[AffectedCitation] = []
            if current is not None:
                used = set(CITATION_PATTERN.findall(current.body))
                affected = [
                    AffectedCitation(
                        handle=handle,
                        version_id=current.id,
                        version_number=current.version_number,
                    )
                    for handle in sorted(remove & used)
                ]
            if affected and not payload.confirm_remove:
                raise SummarySourceRemovalError(affected)
            sources = [source for source in sources if source.handle not in remove]
            connection.execute(
                """
                UPDATE research_summaries
                SET current_sources = ?, next_source_number = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _dumps([source.model_dump(mode="json") for source in sources]),
                    next_number,
                    utc_now(),
                    summary_id,
                ),
            )
        return self.detail(summary_id)

    def save_manual_version(
        self, summary_id: str, payload: SummaryVersionCreate
    ) -> SummaryDetail:
        self._append_version(
            summary_id,
            body=payload.body,
            base_version_id=payload.base_version_id,
            status="edited",
            sources=None,
            coverage=None,
        )
        return self.detail(summary_id)

    def _append_version(
        self,
        summary_id: str,
        *,
        body: str,
        base_version_id: Optional[str],
        status: str,
        sources: Optional[List[SummarySource]],
        coverage: Optional[Dict[str, Any]],
        task_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        generator_provider: Optional[str] = None,
        requested_provider: Optional[str] = None,
        actual_model: Optional[str] = None,
    ) -> SummaryVersion:
        version_id = new_id()
        now = utc_now()
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            summary = self._get_in_connection(connection, summary_id)
            self._require_case(connection, summary.case_id, writable=True)
            base_version = None
            if base_version_id is not None:
                base = connection.execute(
                    """
                    SELECT * FROM research_summary_versions
                    WHERE id = ? AND summary_id = ?
                    """,
                    (base_version_id, summary_id),
                ).fetchone()
                if base is None:
                    raise SummaryValidationError("基线版本不存在或不属于当前摘要")
                base_version = self._version_from_row(base)
            selected = sources if sources is not None else summary.current_sources
            citations = (
                dict(base_version.citations)
                if sources is None and base_version is not None
                else {}
            )
            citations.update({source.handle: source for source in selected})
            version_number = connection.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1 AS number
                FROM research_summary_versions WHERE summary_id = ?
                """,
                (summary_id,),
            ).fetchone()["number"]
            version_coverage = (
                coverage
                if coverage is not None
                else coverage_for_sources(selected)
            )
            connection.execute(
                """
                INSERT INTO research_summary_versions (
                    id, summary_id, version_number, status, body, citations,
                    base_version_id, task_id, attempt_id, generator_provider,
                    requested_provider, actual_model, coverage, saved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    summary_id,
                    version_number,
                    status,
                    body,
                    _dumps(
                        {
                            handle: source.model_dump(mode="json")
                            for handle, source in citations.items()
                        }
                    ),
                    base_version_id,
                    task_id,
                    attempt_id,
                    generator_provider,
                    requested_provider,
                    actual_model,
                    _dumps(version_coverage),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE research_summaries
                SET current_version_id = ?, updated_at = ? WHERE id = ?
                """,
                (version_id, now, summary_id),
            )
            row = connection.execute(
                "SELECT * FROM research_summary_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return self._version_from_row(row)

    def append_generated_version(
        self,
        summary_id: str,
        *,
        body: str,
        base_version_id: Optional[str],
        sources: List[SummarySource],
        coverage: Dict[str, Any],
        task_id: str,
        attempt_id: str,
        provider: str,
        actual_model: Optional[str],
        warnings: List[str],
    ) -> SummaryVersion:
        version_coverage = dict(coverage)
        version_coverage["generation_warnings"] = warnings
        return self._append_version(
            summary_id,
            body=body,
            base_version_id=base_version_id,
            status="draft",
            sources=sources,
            coverage=version_coverage,
            task_id=task_id,
            attempt_id=attempt_id,
            generator_provider=provider,
            requested_provider=provider,
            actual_model=actual_model,
        )

    def confirm(self, summary_id: str) -> SummaryDetail:
        now = utc_now()
        with self.connect() as connection:
            summary = self._get_in_connection(connection, summary_id)
            self._require_case(connection, summary.case_id, writable=True)
            if summary.current_version_id is None:
                raise SummaryConfirmationError(self.validate_version(summary, None))
            row = connection.execute(
                "SELECT * FROM research_summary_versions WHERE id = ?",
                (summary.current_version_id,),
            ).fetchone()
            version = self._version_from_row(row)
            issues = self.validate_version(summary, version)
            if issues:
                raise SummaryConfirmationError(issues)
            connection.execute(
                """
                UPDATE research_summary_versions
                SET status = 'confirmed', confirmed_at = ? WHERE id = ?
                """,
                (now, version.id),
            )
            connection.execute(
                "UPDATE research_summaries SET updated_at = ? WHERE id = ?",
                (now, summary_id),
            )
        return self.detail(summary_id)

    def attach_task(self, summary_id: str, task_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE research_summaries SET latest_task_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (task_id, utc_now(), summary_id),
            )


class SummaryHandler:
    def __init__(
        self,
        summaries: SummaryRepository,
        tasks: TaskRepository,
        gateway,
        provider: Optional[str] = None,
    ) -> None:
        self.summaries = summaries
        self.tasks = tasks
        self.gateway = gateway
        self.provider = provider or os.getenv("SLP_SUMMARY_PROVIDER", "builtin-extractive")
        if self.provider not in SUMMARY_PROVIDERS:
            raise ValueError("SLP_SUMMARY_PROVIDER 配置无效")

    def prepare(self, summary_id: str) -> Dict[str, Any]:
        detail = self.summaries.detail(summary_id)
        summary = detail.summary
        self.summaries.require_case(summary.case_id, writable=True)
        if not detail.sources:
            raise SummaryValidationError("请先选择证据或材料段落，也可以手工编写摘要")
        return {
            "summary_id": summary.id,
            "question": summary.question or summary.title,
            "sources": [
                source.model_dump(mode="json") for source in detail.sources
            ],
            "base_version_id": summary.current_version_id,
            "provider": self.provider,
            "coverage": coverage_for_sources(detail.sources),
        }

    def normalize_retry(self, scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if scope is not None:
            raise SummaryValidationError("摘要生成重试不支持修改输入快照")
        return None

    async def __call__(
        self, context: TaskExecutionContext, input_snapshot: Dict[str, Any]
    ) -> TaskOutcome:
        summary_id = str(input_snapshot.get("summary_id") or "")
        detail = self.summaries.detail(summary_id)
        task = self.tasks.get(context.task_id)
        if task.case_id != detail.summary.case_id:
            raise SummaryValidationError("摘要与任务不属于同一案件")
        sources = [SummarySource(**item) for item in input_snapshot.get("sources") or []]
        if not sources:
            raise SummaryValidationError("生成快照没有可用来源")
        provider = str(input_snapshot.get("provider") or "")
        if provider not in SUMMARY_PROVIDERS:
            raise SummaryValidationError("生成器配置无效")
        question = str(input_snapshot.get("question") or "").strip()
        if not question:
            raise SummaryValidationError("研究问题不能为空")
        base_version_id = input_snapshot.get("base_version_id")
        coverage = input_snapshot.get("coverage") or coverage_for_sources(sources)
        await context.report(
            current_stage="正在生成研究摘要",
            progress_detail="已冻结研究问题与来源，等待生成器返回",
        )
        response = await self.gateway.generate_summary(
            {
                "question": question,
                "sources": [source.model_dump(mode="json") for source in sources],
                "provider": provider,
            }
        )
        body = response.get("body")
        used_handles = response.get("used_handles")
        response_provider = response.get("provider")
        actual_model = response.get("actual_model")
        warnings = response.get("warnings") or []
        if not isinstance(body, str) or not body.strip():
            raise SummaryValidationError("生成服务返回了空白摘要")
        if not isinstance(used_handles, list) or not all(
            isinstance(handle, str) for handle in used_handles
        ):
            raise SummaryValidationError("生成服务返回了无效引用列表")
        allowed = {source.handle for source in sources}
        cited = set(CITATION_PATTERN.findall(body))
        if not cited <= allowed or not set(used_handles) <= allowed:
            raise SummaryValidationError("生成服务返回了未知或跨来源引用")
        if response_provider != provider:
            raise SummaryValidationError("生成服务返回了不匹配的生成器标识")
        if provider == "builtin-extractive" and actual_model is not None:
            raise SummaryValidationError("规则生成器不应声明实际模型")
        if provider == "openai-compatible" and not str(actual_model or "").strip():
            raise SummaryValidationError("真实模型生成结果缺少实际模型标识")
        if not isinstance(warnings, list) or not all(
            isinstance(warning, str) for warning in warnings
        ):
            raise SummaryValidationError("生成服务返回了无效警告")
        await context.checkpoint()
        version = self.summaries.append_generated_version(
            summary_id,
            body=body,
            base_version_id=base_version_id,
            sources=sources,
            coverage=coverage,
            task_id=context.task_id,
            attempt_id=context.attempt_id,
            provider=provider,
            actual_model=actual_model,
            warnings=warnings,
        )
        return TaskOutcome(
            current_stage="研究摘要已生成",
            progress_detail=f"已保存第 {version.version_number} 版规则或模型草稿，请人工复核",
            result={
                "summary_id": summary_id,
                "version_id": version.id,
                "version_number": version.version_number,
                "provider": provider,
                "actual_model": actual_model,
                "used_handles": used_handles,
                "warnings": warnings,
            },
        )
