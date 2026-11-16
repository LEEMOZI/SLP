import asyncio
import json
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, StrictInt, constr, model_validator

from .cases import utc_now
from .materials import MaterialNotFoundError, MaterialRepository
from .task_service import TaskCancelled, TaskExecutionContext
from .tasks import Task, TaskOutcome, TaskRepository, loads, new_id


class SearchFilters(BaseModel):
    year_from: Optional[StrictInt] = Field(default=None, ge=1000, le=9999)
    year_to: Optional[StrictInt] = Field(default=None, ge=1000, le=9999)
    court: constr(strip_whitespace=True, max_length=200) = ""
    cause: constr(strip_whitespace=True, max_length=200) = ""

    @model_validator(mode="after")
    def valid_year_range(self):
        if self.year_from is not None and self.year_to is not None:
            if self.year_from > self.year_to:
                raise ValueError("起始年份不能晚于结束年份")
        return self


class SearchCreate(BaseModel):
    question: constr(strip_whitespace=True, min_length=1, max_length=2000)
    query: Optional[constr(strip_whitespace=True, min_length=1, max_length=2000)] = None
    mode: Literal["auto", "law", "cases"] = "auto"
    material_version_ids: List[str] = Field(default_factory=list, max_length=10)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    limit: StrictInt = Field(default=10, ge=1, le=50)


class EvidenceItem(BaseModel):
    id: str
    case_id: str
    task_id: str
    attempt_id: str
    group_name: str
    source: str
    title: str
    source_label: str
    excerpt: str
    content: str
    hit_reason: str
    match_type: str
    partial_match: bool
    rank: int
    score: Optional[float] = None
    locator: Dict[str, Any]
    source_ref: Dict[str, Any]
    metadata: Dict[str, Any]
    created_at: str


class SearchHistory(BaseModel):
    items: List[Task]
    total: int


class SearchAttemptView(BaseModel):
    task_id: str
    attempt_id: str
    status: str
    plan: Dict[str, Any]
    sources: Dict[str, Any]
    evidence: Dict[str, List[EvidenceItem]]
    error: Optional[str] = None


class EvidenceDetail(BaseModel):
    evidence: EvidenceItem
    document: Optional[Dict[str, Any]] = None
    source_error: Optional[str] = None


class EvidenceNotFoundError(Exception):
    pass


class SearchNotFoundError(Exception):
    pass


class SearchValidationError(Exception):
    pass


class AllSourcesFailed(Exception):
    pass


def _json(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _evidence_from_row(row) -> EvidenceItem:
    values = {key: row[key] for key in EvidenceItem.model_fields}
    values["partial_match"] = bool(values["partial_match"])
    for key in ("locator", "source_ref", "metadata"):
        values[key] = loads(values[key]) or {}
    return EvidenceItem(**values)


class EvidenceRepository:
    def __init__(self, case_repository):
        self.case_repository = case_repository

    def connect(self):
        return self.case_repository.connect()

    def save_source(
        self,
        *,
        case_id: str,
        task_id: str,
        attempt_id: str,
        source: str,
        results: List[Dict[str, Any]],
    ) -> List[EvidenceItem]:
        now = utc_now()
        group_name = {
            "law": "law",
            "judgments": "cases",
            "guiding_cases": "cases",
            "reference": "reference",
        }[source]
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM evidence_items WHERE attempt_id = ? AND source = ?",
                (attempt_id, source),
            )
            for item in results:
                connection.execute(
                    """
                    INSERT INTO evidence_items (
                        id, case_id, task_id, attempt_id, group_name, source, title,
                        source_label, excerpt, content, hit_reason, match_type,
                        partial_match, rank, score, locator, source_ref, metadata, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id(),
                        case_id,
                        task_id,
                        attempt_id,
                        group_name,
                        source,
                        str(item.get("title") or "未命名证据"),
                        str(item.get("source_label") or source),
                        str(item.get("excerpt") or ""),
                        str(item.get("content") or ""),
                        str(item.get("hit_reason") or "命中原因未提供"),
                        str(item.get("match_type") or "unknown"),
                        bool(item.get("partial_match") or item.get("match_type") == "or"),
                        int(item.get("rank") or 0),
                        item.get("score"),
                        _json(item.get("locator") or {}),
                        _json(item.get("source_ref") or {}),
                        _json(item.get("metadata") or {}),
                        now,
                    ),
                )
            rows = connection.execute(
                "SELECT * FROM evidence_items WHERE attempt_id = ? AND source = ?",
                (attempt_id, source),
            ).fetchall()
        return [_evidence_from_row(row) for row in rows]

    def list_for_attempt(self, attempt_id: str) -> List[EvidenceItem]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM evidence_items WHERE attempt_id = ?
                ORDER BY group_name, source, rank, created_at, id
                """,
                (attempt_id,),
            ).fetchall()
        return [_evidence_from_row(row) for row in rows]

    def get(self, evidence_id: str) -> EvidenceItem:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (evidence_id,)
            ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(evidence_id)
        return _evidence_from_row(row)

    @staticmethod
    def source_key(source_ref: Dict[str, Any]) -> str:
        return json.dumps(source_ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def get_document(self, source: str, source_ref: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = self.source_key(source_ref)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_documents WHERE source = ? AND source_key = ?",
                (source, key),
            ).fetchone()
        if row is None:
            return None
        return {
            "source": source,
            "source_ref": source_ref,
            "title": row["title"],
            "content": row["content"],
            "metadata": loads(row["metadata"]) or {},
            "fetched_at": row["fetched_at"],
            "cached": True,
        }

    def cache_document(
        self,
        source: str,
        source_ref: Dict[str, Any],
        title: str,
        content: str,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        key = self.source_key(source_ref)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO evidence_documents (
                    id, source, source_key, title, content, metadata, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id(), source, key, title, content, _json(metadata), utc_now()),
            )
        return self.get_document(source, source_ref)


class SearchPlanner:
    CASE_WORDS = ("类案", "案例", "判例", "裁判文书", "指导案例", "类似案件")
    LAW_WORDS = ("法条", "法律规定", "法律依据", "定义", "何谓", "什么是", "程序", "管辖", "诉讼时效", "起诉", "立案")
    DISPUTE_WORDS = ("责任", "效力", "赔偿", "违约", "侵权", "解除", "争议", "无效", "有效")
    REFERENCE_WORDS = ("办案指引", "实务指引", "学习", "教程", "工具书")
    DISPUTE_TERMS = ("合同效力", "违约责任", "损害赔偿", "民间借贷", "劳动合同", "侵权责任", "买卖合同", "诉讼时效")
    BOOK_TITLE = re.compile(r"《([^》\n]{2,60})》")

    def __init__(self, materials: MaterialRepository):
        self.materials = materials

    def validate_versions(self, case_id: str, version_ids: List[str]) -> None:
        for version_id in dict.fromkeys(version_ids):
            try:
                self.materials.get_usable_version(case_id, version_id)
            except MaterialNotFoundError as error:
                raise SearchValidationError("所选材料版本不存在、不可用于检索或不属于当前案件") from error

    def _enrich(self, case_id: str, version_ids: List[str]) -> Dict[str, Any]:
        terms: List[str] = []
        traces = []
        for version_id in dict.fromkeys(version_ids):
            self.materials.get_usable_version(case_id, version_id)
            segments = self.materials.get_search_segments(version_id)
            version_terms: List[str] = []
            segment_ids = []
            for segment in segments:
                found = list(self.BOOK_TITLE.findall(segment.text))
                found.extend(term for term in self.DISPUTE_TERMS if term in segment.text)
                if found:
                    segment_ids.append(segment.id)
                for term in found:
                    if term not in version_terms:
                        version_terms.append(term)
                    if term not in terms:
                        terms.append(term)
            traces.append(
                {
                    "material_version_id": version_id,
                    "segment_ids": segment_ids[:30],
                    "terms": version_terms[:8],
                }
            )
        return {"terms": terms[:8], "materials": traces}

    def build(self, case_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        mode = snapshot["mode"]
        question = snapshot["question"]
        base_query = snapshot.get("query") or question
        enrichment = self._enrich(case_id, snapshot.get("material_version_ids") or [])
        query = " ".join([base_query] + [term for term in enrichment["terms"] if term not in base_query])[:2000]
        uncovered: List[str] = []
        if mode == "law":
            sources = ["law"]
            reason = "用户选择法规模式"
        elif mode == "cases":
            sources = ["judgments", "guiding_cases"]
            reason = "用户选择类案模式，裁判文书为主、指导案例为补充"
        elif any(word in question for word in self.REFERENCE_WORDS):
            sources = ["reference"]
            reason = "识别到明确的办案指引或学习意图"
        elif any(word in question for word in self.CASE_WORDS):
            sources = ["judgments", "guiding_cases"]
            reason = "识别到明确类案意图"
        elif any(word in question for word in self.LAW_WORDS) or re.search(r"第[^，。；\n]{1,12}条", question):
            sources = ["law"]
            reason = "识别到法条定位、定义或程序问题"
        elif any(word in question for word in self.DISPUTE_WORDS):
            sources = ["law", "judgments"]
            reason = "识别到争议分析，并行检索法规与裁判文书"
        else:
            sources = ["law"]
            uncovered = ["judgments", "guiding_cases", "reference"]
            reason = "无法明确识别意图，法规优先，未自动覆盖其他来源"
        return {
            "mode": mode,
            "question": question,
            "base_query": base_query,
            "query": query,
            "sources": sources,
            "source_queries": {source: query for source in sources},
            "filters": snapshot["filters"],
            "material_enrichment": enrichment["materials"],
            "uncovered_sources": uncovered,
            "routing_reason": reason,
        }


class SearchHandler:
    def __init__(
        self,
        materials: MaterialRepository,
        evidence: EvidenceRepository,
        tasks: TaskRepository,
        gateway,
    ) -> None:
        self.materials = materials
        self.evidence = evidence
        self.tasks = tasks
        self.gateway = gateway
        self.planner = SearchPlanner(materials)

    def prepare(self, case_id: str, payload: SearchCreate) -> Dict[str, Any]:
        self.materials.require_case(case_id, writable=True)
        self.planner.validate_versions(case_id, payload.material_version_ids)
        return payload.model_dump(mode="json")

    def normalize_retry(
        self, case_id: str, input_snapshot: Dict[str, Any], scope: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if scope is None:
            return None
        allowed = set(SearchCreate.model_fields)
        if set(scope) - allowed:
            raise SearchValidationError("重试范围包含不支持的字段")
        merged = {key: value for key, value in input_snapshot.items() if key != "scope"}
        merged.update(scope)
        try:
            payload = SearchCreate(**merged)
        except ValueError as error:
            raise SearchValidationError("重试参数无效") from error
        self.planner.validate_versions(case_id, payload.material_version_ids)
        normalized = payload.model_dump(mode="json")
        return {key: normalized[key] for key in scope}

    def history(self, case_id: str) -> SearchHistory:
        self.materials.require_case(case_id)
        items = [
            task
            for task in self.tasks.list(case_id=case_id)
            if task.task_type == "search"
        ]
        return SearchHistory(items=items, total=len(items))

    def attempt_view(self, task_id: str, attempt_id: str) -> SearchAttemptView:
        detail = self.tasks.get_detail(task_id)
        if detail.task_type != "search":
            raise SearchNotFoundError(task_id)
        attempt = next((item for item in detail.attempts if item.id == attempt_id), None)
        if attempt is None:
            raise SearchNotFoundError(attempt_id)
        result = attempt.result or {}
        grouped = {"law": [], "cases": [], "reference": []}
        for item in self.evidence.list_for_attempt(attempt_id):
            grouped[item.group_name].append(item)
        return SearchAttemptView(
            task_id=task_id,
            attempt_id=attempt_id,
            status=attempt.status,
            plan=result.get("plan") or {},
            sources=result.get("sources") or {},
            evidence=grouped,
            error=attempt.error,
        )

    async def evidence_detail(self, evidence_id: str) -> EvidenceDetail:
        evidence = self.evidence.get(evidence_id)
        if evidence.source != "judgments":
            return EvidenceDetail(evidence=evidence)
        source_ref = evidence.source_ref
        year = source_ref.get("year")
        rowid = source_ref.get("rowid")
        if not isinstance(year, int) or isinstance(year, bool) or not isinstance(rowid, int) or isinstance(rowid, bool):
            return EvidenceDetail(evidence=evidence, source_error="该证据没有可用的原文定位")
        cached = self.evidence.get_document(evidence.source, source_ref)
        if cached is not None:
            return EvidenceDetail(evidence=evidence, document=cached)
        try:
            document = await self.gateway.judgment_source(year, rowid)
            content = str(document.get("content") or "")
            if not content:
                raise ValueError("empty source")
            cached = self.evidence.cache_document(
                evidence.source,
                source_ref,
                str(document.get("title") or evidence.title),
                content,
                document.get("metadata") or {},
            )
            return EvidenceDetail(evidence=evidence, document=cached)
        except Exception:
            return EvidenceDetail(evidence=evidence, source_error="裁判文书原文暂不可用，可稍后重试")

    async def __call__(
        self, context: TaskExecutionContext, input_snapshot: Dict[str, Any]
    ) -> TaskOutcome:
        task = self.tasks.get(context.task_id)
        merged = {key: value for key, value in input_snapshot.items() if key != "scope"}
        scope = input_snapshot.get("scope")
        if scope:
            merged.update(scope)
        payload = SearchCreate(**merged)
        snapshot = payload.model_dump(mode="json")
        plan = self.planner.build(task.case_id, snapshot)
        states = {
            source: {"source": source, "status": "running", "evidence_count": 0}
            for source in plan["sources"]
        }
        evidence_count = 0

        def current_result() -> Dict[str, Any]:
            available = evidence_count > 0 or any(
                state["status"] in {"success", "partial"} for state in states.values()
            )
            return {
                "plan": plan,
                "sources": json.loads(json.dumps(states, ensure_ascii=False)),
                "evidence_count": evidence_count,
                "has_partial_results": available,
            }

        await context.report_partial(
            current_stage="检索规划完成",
            progress_detail="已冻结检索词与来源计划",
            progress=0,
            result=current_result(),
        )

        async def run_source(source: str) -> None:
            nonlocal evidence_count
            await context.checkpoint()
            filters = plan["filters"] if source == "judgments" else {}
            request = {"query": plan["source_queries"][source], "limit": payload.limit}
            if source == "judgments":
                request.update(filters)
            else:
                request["filters"] = filters
            try:
                response = await self.gateway.search(source, request)
                status = response.get("status")
                if status not in {"success", "partial", "failed"}:
                    raise ValueError("invalid source status")
                results = response.get("results") or []
                if not isinstance(results, list):
                    raise ValueError("invalid source results")
                saved = self.evidence.save_source(
                    case_id=task.case_id,
                    task_id=context.task_id,
                    attempt_id=context.attempt_id,
                    source=source,
                    results=results,
                )
                evidence_count += len(saved)
                states[source] = {
                    "source": source,
                    "status": status,
                    "query": response.get("query") or plan["source_queries"][source],
                    "filters": response.get("filters") or filters,
                    "executed_queries": response.get("executed_queries") or [],
                    "coverage": response.get("coverage") or {},
                    "error": response.get("error"),
                    "evidence_count": len(saved),
                }
            except TaskCancelled:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                states[source] = {
                    "source": source,
                    "status": "failed",
                    "query": plan["source_queries"][source],
                    "filters": filters,
                    "executed_queries": [],
                    "coverage": {"status": "failed"},
                    "error": {"code": "source_unavailable", "message": "检索来源暂不可用"},
                    "evidence_count": 0,
                }
            completed = sum(state["status"] != "running" for state in states.values())
            await context.report_partial(
                current_stage=f"{source} 检索已返回",
                progress_detail=f"已完成 {completed}/{len(states)} 个来源，保存 {evidence_count} 条证据",
                progress=int(completed * 90 / len(states)),
                result=current_result(),
            )

        workers = [asyncio.create_task(run_source(source)) for source in plan["sources"]]
        try:
            await asyncio.gather(*workers)
        except (TaskCancelled, asyncio.CancelledError):
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        result = current_result()
        if all(state["status"] == "failed" for state in states.values()):
            raise AllSourcesFailed("所有检索来源均不可用，未获得可用结果")
        partial = any(state["status"] != "success" for state in states.values())
        return TaskOutcome(
            status="partial" if partial else "completed",
            current_stage="部分检索完成" if partial else "检索完成",
            progress_detail=(
                "部分来源失败或覆盖不完整，已保留可用证据"
                if partial
                else f"检索完成，共保存 {evidence_count} 条证据"
            ),
            progress=100,
            result=result,
        )
