"""P1 诉讼文书起草：案情输入 → 法条检索 → LLM 生成 → 引用校验 → 版本管理与交付。

复用合同审查的版本管理模式：draft / edited / confirmed + 版本号不可变 + 交付导出。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .cases import CaseArchivedError, CaseRepository, utc_now
from .contract_reviews import LawSource, check_citations
from .summaries import CITATION_PATTERN, CitationIssue
from .tasks import TaskCaseNotFoundError, new_id


# ---- 文书类型 ----

DOC_TYPES = ("complaint", "defense", "representation", "legal_opinion")
DOC_TYPE_LABELS = {
    "complaint": "民事起诉状",
    "defense": "民事答辩状",
    "representation": "代理词",
    "legal_opinion": "法律意见书",
}


# ---- Pydantic 模型 ----

class LitigationDocument(BaseModel):
    id: str
    case_id: str
    doc_type: str
    title: str
    parties: str
    claims: str
    facts: str
    focus_points: str
    current_version_id: Optional[str] = None
    current_version_number: Optional[int] = None
    current_sources: List[LawSource] = Field(default_factory=list)
    latest_task_id: Optional[str] = None
    created_at: str
    updated_at: str


class LitigationDocumentVersion(BaseModel):
    id: str
    document_id: str
    version_number: int
    status: Literal["draft", "edited", "confirmed"]
    body: str
    citations: Dict[str, LawSource]
    base_version_id: Optional[str] = None
    task_id: Optional[str] = None
    attempt_id: Optional[str] = None
    generator_provider: Optional[str] = None
    requested_provider: Optional[str] = None
    actual_model: Optional[str] = None
    coverage: Dict[str, Any] = Field(default_factory=dict)
    saved_at: str
    confirmed_at: Optional[str] = None


class LitigationDocumentList(BaseModel):
    items: List[LitigationDocument]
    total: int


class LitigationDocumentDetail(BaseModel):
    document: LitigationDocument
    sources: List[LawSource]
    current_version: Optional[LitigationDocumentVersion] = None
    versions: List[LitigationDocumentVersion]
    issues: List[CitationIssue] = Field(default_factory=list)


class LitigationDocumentCreate(BaseModel):
    doc_type: str = Field(min_length=1, max_length=40)
    title: str = Field(default="诉讼文书", min_length=1, max_length=160)
    parties: str = Field(default="", max_length=2000)
    claims: str = Field(default="", max_length=10000)
    facts: str = Field(default="", max_length=50000)
    focus_points: str = Field(default="", max_length=2000)


class LitigationDocumentVersionCreate(BaseModel):
    body: str = Field(default="", max_length=200000)
    base_version_id: Optional[str] = None


# ---- 异常 ----

class LitigationDocumentNotFoundError(Exception):
    pass


class LitigationDocumentValidationError(Exception):
    pass


class LitigationDocumentConfirmationError(Exception):
    def __init__(self, issues: List[CitationIssue]) -> None:
        super().__init__("诉讼文书仍存在引用或内容问题")
        self.issues = issues


# ---- 工具函数 ----

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Optional[str], default: Any) -> Any:
    return json.loads(value) if value is not None else default


# ---- Repository ----

class LitigationDocumentRepository:
    def __init__(self, case_repository: CaseRepository) -> None:
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

    @staticmethod
    def _doc_from_row(row) -> LitigationDocument:
        values = dict(row)
        values["current_sources"] = [
            LawSource(**item)
            for item in _loads(values.get("current_sources"), [])
        ]
        return LitigationDocument(**values)

    @staticmethod
    def _version_from_row(row) -> LitigationDocumentVersion:
        values = dict(row)
        values["citations"] = {
            handle: LawSource(**source)
            for handle, source in _loads(values["citations"], {}).items()
        }
        values["coverage"] = _loads(values["coverage"], {})
        return LitigationDocumentVersion(**values)

    def create(
        self,
        case_id: str,
        *,
        doc_type: str,
        title: str = "诉讼文书",
        parties: str = "",
        claims: str = "",
        facts: str = "",
        focus_points: str = "",
    ) -> LitigationDocument:
        if doc_type not in DOC_TYPES:
            raise LitigationDocumentValidationError(
                f"不支持的文书类型：{doc_type}，可选：{', '.join(DOC_TYPES)}"
            )
        self.require_case(case_id, writable=True)
        with self.connect() as connection:
            doc_id = new_id()
            now = utc_now()
            connection.execute(
                """
                INSERT INTO litigation_documents(
                    id, case_id, doc_type, title, parties, claims, facts,
                    focus_points, current_version_id, current_version_number,
                    current_sources, latest_task_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '[]', NULL, ?, ?)
                """,
                (
                    doc_id,
                    case_id,
                    doc_type,
                    title.strip(),
                    parties.strip(),
                    claims.strip(),
                    facts.strip(),
                    focus_points.strip(),
                    now,
                    now,
                ),
            )
        return self.get(doc_id)

    def get(self, doc_id: str) -> LitigationDocument:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT d.*, v.version_number AS current_version_number
                FROM litigation_documents d
                LEFT JOIN litigation_document_versions v ON v.id = d.current_version_id
                WHERE d.id = ?
                """,
                (doc_id,),
            ).fetchone()
        if row is None:
            raise LitigationDocumentNotFoundError(doc_id)
        return self._doc_from_row(row)

    def list(self, case_id: str) -> List[LitigationDocument]:
        self.require_case(case_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, v.version_number AS current_version_number
                FROM litigation_documents d
                LEFT JOIN litigation_document_versions v ON v.id = d.current_version_id
                WHERE d.case_id = ?
                ORDER BY d.updated_at DESC, d.id DESC
                """,
                (case_id,),
            ).fetchall()
        return [self._doc_from_row(row) for row in rows]

    def detail(self, doc_id: str) -> LitigationDocumentDetail:
        doc = self.get(doc_id)
        with self.connect() as connection:
            versions = [
                self._version_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM litigation_document_versions
                    WHERE document_id = ? ORDER BY version_number DESC
                    """,
                    (doc_id,),
                ).fetchall()
            ]
            current = None
            for version in versions:
                if version.id == doc.current_version_id:
                    current = version
                    break
        sources = doc.current_sources
        if current is not None:
            issues = validate_version(doc, current)
        else:
            issues = []
        return LitigationDocumentDetail(
            document=doc,
            sources=sources,
            current_version=current,
            versions=versions,
            issues=issues,
        )

    def append_generated_version(
        self,
        doc_id: str,
        *,
        body: str,
        citations: Dict[str, LawSource],
        provider: Optional[str] = None,
        requested_provider: Optional[str] = None,
        actual_model: Optional[str] = None,
        task_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        sources: Optional[List[LawSource]] = None,
    ) -> LitigationDocumentVersion:
        doc = self.get(doc_id)
        if sources is not None:
            self._set_current_sources(doc.case_id, doc_id, sources)
        with self.connect() as connection:
            new_number = (doc.current_version_number or 0) + 1
            now = utc_now()
            version_id = new_id()
            connection.execute(
                """
                INSERT INTO litigation_document_versions(
                    id, document_id, version_number, status, body, citations,
                    base_version_id, task_id, attempt_id, generator_provider,
                    requested_provider, actual_model, coverage, saved_at, confirmed_at
                ) VALUES (?, ?, ?, 'draft', ?, ?, NULL, ?, ?, ?, ?, ?, '{}', ?, NULL)
                """,
                (
                    version_id,
                    doc_id,
                    new_number,
                    body or "",
                    _dumps({k: v.model_dump() for k, v in citations.items()}),
                    task_id,
                    attempt_id,
                    provider,
                    requested_provider,
                    actual_model,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE litigation_documents
                SET current_version_id = ?, current_version_number = ?,
                    latest_task_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (version_id, new_number, task_id, now, doc_id),
            )
        return self._fetch_version(version_id)

    def save_manual_version(
        self, doc_id: str, *, body: str, base_version_id: Optional[str] = None
    ) -> LitigationDocumentVersion:
        doc = self.get(doc_id)
        with self.connect() as connection:
            new_number = (doc.current_version_number or 0) + 1
            now = utc_now()
            version_id = new_id()
            parent_citations = {}
            if doc.current_version_id:
                parent_row = connection.execute(
                    "SELECT citations FROM litigation_document_versions WHERE id = ?",
                    (doc.current_version_id,),
                ).fetchone()
                if parent_row is not None:
                    parent_citations = _loads(parent_row["citations"], {})
            connection.execute(
                """
                INSERT INTO litigation_document_versions(
                    id, document_id, version_number, status, body, citations,
                    base_version_id, task_id, attempt_id, generator_provider,
                    requested_provider, actual_model, coverage, saved_at, confirmed_at
                ) VALUES (?, ?, ?, 'edited', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, '{}', ?, NULL)
                """,
                (
                    version_id,
                    doc_id,
                    new_number,
                    body or "",
                    _dumps(parent_citations),
                    base_version_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE litigation_documents
                SET current_version_id = ?, current_version_number = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (version_id, new_number, now, doc_id),
            )
        return self._fetch_version(version_id)

    def confirm(self, doc_id: str) -> LitigationDocumentDetail:
        detail = self.detail(doc_id)
        version = detail.current_version
        if version is None:
            raise LitigationDocumentValidationError("当前没有可确认的版本")
        issues = validate_version(detail.document, version)
        if issues:
            raise LitigationDocumentConfirmationError(issues)
        warnings = check_citations(version.body, [
            {"law_name": s.law_name, "article": s.article}
            for s in version.citations.values()
        ])
        with self.connect() as connection:
            now = utc_now()
            connection.execute(
                """
                UPDATE litigation_document_versions
                SET status = 'confirmed', confirmed_at = ?
                WHERE id = ?
                """,
                (now, version.id),
            )
        return self.detail(doc_id)

    def _set_current_sources(
        self, case_id: str, doc_id: str, sources: List[LawSource]
    ) -> None:
        ordered = sorted(sources, key=lambda s: (s.sequence or 0, s.handle))
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE litigation_documents
                SET current_sources = ?, updated_at = ?
                WHERE id = ?
                """,
                (_dumps([s.model_dump() for s in ordered]), utc_now(), doc_id),
            )

    def _fetch_version(self, version_id: str) -> LitigationDocumentVersion:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM litigation_document_versions WHERE id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise LitigationDocumentNotFoundError(version_id)
        return self._version_from_row(row)


# ---- 版本校验 ----

def validate_version(
    doc: LitigationDocument, version: LitigationDocumentVersion
) -> List[CitationIssue]:
    issues: List[CitationIssue] = []
    body = version.body or ""
    handles_in_body = {match.group(1) for match in CITATION_PATTERN.finditer(body)}
    if not body.strip():
        issues.append(CitationIssue(code="empty_document", message="诉讼文书正文为空"))
        return issues
    if not handles_in_body:
        issues.append(CitationIssue(
            code="no_citation",
            message="文书未引用任何法条来源（[[handle]] 形式），请补充法条引用后再确认",
        ))
        return issues
    for handle in handles_in_body:
        if handle not in version.citations:
            issues.append(CitationIssue(
                code="unknown_citation",
                handle=handle,
                message=f"正文引用的 [[{handle}]] 未在本次法条来源中找到",
            ))
    return issues


# ---- Handler ----

class LitigationHandler:
    def __init__(
        self,
        documents: LitigationDocumentRepository,
        tasks,
        gateway,
    ) -> None:
        self.documents = documents
        self.tasks = tasks
        self.gateway = gateway

    def prepare(self, doc_id: str) -> Dict[str, Any]:
        detail = self.documents.detail(doc_id)
        doc = detail.document
        self.documents.require_case(doc.case_id, writable=True)
        return {
            "document_id": doc.id,
            "doc_type": doc.doc_type,
            "title": doc.title,
            "parties": doc.parties,
            "claims": doc.claims,
            "facts": doc.facts,
            "focus_points": doc.focus_points,
        }

    def normalize_retry(self, scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if scope is not None:
            raise LitigationDocumentValidationError("诉讼文书重试沿用原快照，暂不支持修改输入")
        return None

    async def __call__(self, context, input_snapshot: Dict[str, Any]) -> Any:
        doc_id = str(input_snapshot.get("document_id") or "")
        detail = self.documents.detail(doc_id)
        task = self.tasks.get(context.task_id)
        if task.case_id != detail.document.case_id:
            raise LitigationDocumentValidationError("诉讼文书与任务不属于同一案件")

        await context.report(
            current_stage="正在起草诉讼文书",
            progress_detail="已冻结案情快照，等待法条检索与模型返回",
        )

        response = await self.gateway.generate_litigation({
            "doc_type": str(input_snapshot.get("doc_type") or ""),
            "title": str(input_snapshot.get("title") or ""),
            "parties": str(input_snapshot.get("parties") or ""),
            "claims": str(input_snapshot.get("claims") or ""),
            "facts": str(input_snapshot.get("facts") or "")[:80000],
            "focus_points": str(input_snapshot.get("focus_points") or ""),
            "top_k": 8,
            "quality": False,
        })

        document = str(response.get("document") or "").strip()
        raw_citations = response.get("citations") or {}
        if not document:
            raise LitigationDocumentValidationError("诉讼文书生成返回了空内容")

        await context.report(
            current_stage="正在写入文书版本",
            progress_detail=f"已获取 {len(raw_citations)} 个法条来源",
        )

        citations: Dict[str, LawSource] = {}
        law_sources: List[LawSource] = []
        for handle, item in raw_citations.items():
            source = LawSource(**{
                **item,
                "source_id": item.get("source_id") or handle,
                "case_id": detail.document.case_id,
            })
            citations[handle] = source
            law_sources.append(source)

        version = self.documents.append_generated_version(
            doc_id,
            body=document,
            citations=citations,
            provider="openai-compatible",
            actual_model=None,
            task_id=context.task_id,
            attempt_id=context.attempt_id,
            sources=law_sources,
        )
        from .tasks import TaskOutcome

        return TaskOutcome(
            current_stage="诉讼文书已生成",
            progress_detail=(
                f"已保存第 {version.version_number} 版，"
                f"共 {len(raw_citations)} 条法条来源"
            ),
            result={
                "document_id": doc_id,
                "version_id": version.id,
                "version_number": version.version_number,
                "citation_count": len(raw_citations),
            },
        )
