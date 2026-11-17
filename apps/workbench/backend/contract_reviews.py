"""FR-08 合同审查：案件材料 → 法条检索 → 风险清单 → 版本管理与交付。

复用摘要版本模式：draft / edited / confirmed + 版本号不可变 + 交付导出。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .cases import CaseArchivedError, CaseRepository, utc_now
from .summaries import CITATION_PATTERN, CitationIssue, SummarySource
from .tasks import TaskCaseNotFoundError, new_id


# ---- check_citations：从 MCP law-search/core/litigation_builder.py 复制 ----
# （工作台 venv 不依赖 Chromadb / LM Studio，纯函数无外部依赖，注明来源）
# ----------------------------------------------------------------------------

_CITE_LAW_RE = re.compile(r"《([^《》]{2,40})》")
_CITE_ART_RE = re.compile(r"第([0-9一二三四五六七八九十百千]+)条")
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(num_str: str) -> Optional[int]:
    s = (num_str or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)

    def segment(seg: str) -> int:
        if "十" in seg:
            left, _, right = seg.partition("十")
            tens = _CN_DIGITS.get(left, 1) if left else 1
            ones = _CN_DIGITS.get(right, 0) if right else 0
            return tens * 10 + ones
        return _CN_DIGITS.get(seg, -1)

    try:
        if "百" in s:
            left, _, rest = s.partition("百")
            hundreds = _CN_DIGITS.get(left, 1) if left else 1
            if hundreds < 0:
                return None
            return hundreds * 100 + (segment(rest) if rest else 0)
        return segment(s)
    except Exception:
        return None


def _norm_article(article: str) -> Optional[int]:
    s = (article or "").strip().removeprefix("第").removesuffix("条").strip()
    return _cn_to_int(s)


def _law_matches(cite_law: str, known_law: str) -> bool:
    a, b = cite_law.strip(), known_law.strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def extract_cited_articles(text: str) -> List:
    """从文书提取（《法名》, 条号 int）列表，按出现顺序去重。"""
    out: List = []
    seen: set = set()
    src = text or ""
    laws = list(_CITE_LAW_RE.finditer(src))
    for i, m in enumerate(laws):
        law = m.group(1).strip()
        start = m.end()
        end = laws[i + 1].start() if i + 1 < len(laws) else len(src)
        for am in _CITE_ART_RE.finditer(src, start, end):
            num = _norm_article(am.group(1))
            if num is None:
                continue
            key = (law, num)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def check_citations(document: str, evidence_pack: List[Dict[str, Any]]) -> List[str]:
    """引用一致性校验（复制自 MCP law-search/core/litigation_builder.py）。

    返回 warning 列表（soft，不阻断确认）：
    - 文书引用了检索之外的法律
    - 法律命中但条号未命中
    """
    if not (document or "").strip():
        return ["合同审查报告为空，未做法条引用校验。"]

    known: set = set()
    for ev in evidence_pack or []:
        law = (ev.get("law_name") or "").strip()
        num = _norm_article(str(ev.get("article") or ""))
        if law:
            known.add((law, num))
    known_laws = list({law for law, _ in known})

    warnings: List[str] = []
    for law, num in extract_cited_articles(document):
        if not any(_law_matches(law, k) for k in known_laws):
            warnings.append(
                f"引用了检索结果之外的法律：《{law}》第{num}条 — 请人工核实该条文是否真实存在及现行有效"
            )
        elif not any(
            _law_matches(law, l) and n == num
            for l, n in known
            if n is not None
        ):
            warnings.append(
                f"《{law}》第{num}条未在本次检索证据中出现 — 请核实条号及内容是否与案情匹配"
            )
    return warnings


# ---- 风险项结构化（从 build_review_prompt 的四级结构解析） ----

RISK_LEVELS = ("高危", "中等", "提示")
RISK_LEVEL_KEYWORDS = {"高危", "高风险", "中等风险", "中等", "提示风险", "提示"}


def parse_risks_from_body(body: str) -> List[Dict[str, Any]]:
    """从合同审查报告的"二、风险清单"章节解析结构化风险项。

    保底策略：按行扫描匹配三级关键词，尝试提取条款引用/风险说明/修改建议/法律依据。
    解析失败时返回空列表，前端仍可显示完整 body。
    """
    if not body:
        return []

    risks: List[Dict[str, Any]] = []
    current_level: Optional[str] = None
    current_risk: Optional[Dict[str, Any]] = None

    def _finalize():
        nonlocal current_risk
        if current_risk is not None and current_risk.get("excerpt"):
            risks.append(current_risk)
        current_risk = None

    for raw_line in (body or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 匹配风险三级标题
        if any(keyword in line for keyword in RISK_LEVEL_KEYWORDS):
            _finalize()
            for level in RISK_LEVELS:
                if level in line:
                    current_level = level
                    break
            # 尝试在同一行提取风险项
            m = re.search(r"风险条款[：:]\s*(.+?)(?:[；;]\s*风险说明|$)", line)
            if m and current_level:
                current_risk = {"level": current_level, "excerpt": m.group(1).strip()}
            continue
        # 子字段匹配
        if current_level and current_risk is not None:
            for field, pattern in (
                ("excerpt", r"风险条款[：:]\s*(.+)"),
                ("risk_issue", r"风险说明[：:]\s*(.+)"),
                ("suggestion", r"修改建议[：:]\s*(.+)"),
                ("legal_basis", r"法律依据[：:]\s*(.+)"),
            ):
                m_field = re.match(pattern, line)
                if m_field and not current_risk.get(field):
                    current_risk[field] = m_field.group(1).strip()
                    break
            continue
        # 普通行且在二级风险区块
        if current_level and current_risk is None and re.match(r"^[-*]\s*(.+)", line):
            excerpt = re.match(r"^[-*]\s*(.+)", line).group(1).strip()
            current_risk = {"level": current_level, "excerpt": excerpt}
            continue

    _finalize()
    # 补齐空字段
    for risk in risks:
        risk.setdefault("risk_issue", "")
        risk.setdefault("suggestion", "")
        risk.setdefault("legal_basis", "")
    return risks


# ---- 契约与 Repository ----

class RiskItem(BaseModel):
    level: Literal["高危", "中等", "提示"]
    excerpt: str = ""
    risk_issue: str = ""
    suggestion: str = ""
    legal_basis: str = ""


class LawSource(SummarySource):
    """法条来源：SummarySource 的特化变体；law_name / article / law_type 附加字段。"""
    law_name: str = ""
    article: Optional[int] = None
    law_type: str = ""


class ContractReviewVersion(BaseModel):
    id: str
    review_id: str
    version_number: int
    status: Literal["draft", "edited", "confirmed"]
    body: str
    risks: List[RiskItem] = Field(default_factory=list)
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


class ContractReview(BaseModel):
    id: str
    case_id: str
    material_id: str
    title: str
    contract_type: str
    focus_points: str
    current_version_id: Optional[str] = None
    current_version_number: Optional[int] = None
    current_sources: List[LawSource] = Field(default_factory=list)
    latest_task_id: Optional[str] = None
    created_at: str
    updated_at: str


class ContractReviewList(BaseModel):
    items: List[ContractReview]
    total: int


class ContractReviewDetail(BaseModel):
    review: ContractReview
    sources: List[LawSource]
    current_version: Optional[ContractReviewVersion] = None
    versions: List[ContractReviewVersion]
    coverage: Dict[str, Any] = Field(default_factory=dict)
    issues: List[CitationIssue] = Field(default_factory=list)


class ContractReviewCreate(BaseModel):
    material_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="合同审查", min_length=1, max_length=160)
    contract_type: str = Field(default="", max_length=80)
    focus_points: str = Field(default="", max_length=1000)


class ContractReviewSourcesUpdate(BaseModel):
    add: List[str] = Field(default_factory=list, max_length=100)
    remove: List[str] = Field(default_factory=list, max_length=100)
    confirm_remove: bool = False


class ContractReviewVersionCreate(BaseModel):
    body: str = Field(default="", max_length=200000)
    base_version_id: Optional[str] = None


class ContractReviewNotFoundError(Exception):
    pass


class ContractReviewValidationError(Exception):
    pass


class ContractReviewConfirmationError(Exception):
    def __init__(self, issues: List[CitationIssue]) -> None:
        super().__init__("合同审查仍存在引用或内容问题")
        self.issues = issues


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Optional[str], default: Any) -> Any:
    return json.loads(value) if value is not None else default


class ContractReviewRepository:
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
    def _review_from_row(row) -> ContractReview:
        values = dict(row)
        values["current_sources"] = [
            LawSource(**item)
            for item in _loads(values.get("current_sources"), [])
        ]
        return ContractReview(**values)

    @staticmethod
    def _version_from_row(row) -> ContractReviewVersion:
        values = dict(row)
        values["risks"] = [
            RiskItem(**item) for item in _loads(values.get("risks"), [])
        ]
        values["citations"] = {
            handle: LawSource(**source)
            for handle, source in _loads(values["citations"], {}).items()
        }
        values["coverage"] = _loads(values["coverage"], {})
        return ContractReviewVersion(**values)

    def create(
        self,
        case_id: str,
        material_id: str,
        *,
        title: str = "合同审查",
        contract_type: str = "",
        focus_points: str = "",
    ) -> ContractReview:
        self.require_case(case_id, writable=True)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT m.id FROM materials m
                JOIN material_parse_versions v ON v.material_id = m.id
                JOIN material_segments seg ON seg.parse_version_id = v.id
                WHERE m.id = ? AND m.case_id = ?
                  AND v.status IN ('success', 'partial')
                  AND seg.status IN ('ok', 'partial')
                LIMIT 1
                """,
                (material_id, case_id),
            ).fetchone()
            if row is None:
                raise ContractReviewValidationError(
                    "材料不存在、未解析完成或不属于当前案件"
                )
            review_id = new_id()
            now = utc_now()
            connection.execute(
                """
                INSERT INTO contract_reviews(
                    id, case_id, material_id, title, contract_type, focus_points,
                    current_version_id, current_version_number, current_sources,
                    latest_task_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, '[]', NULL, ?, ?)
                """,
                (
                    review_id,
                    case_id,
                    material_id,
                    title.strip(),
                    contract_type.strip(),
                    focus_points.strip(),
                    now,
                    now,
                ),
            )
        return self.get(review_id)

    def get(self, review_id: str) -> ContractReview:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT r.*, v.version_number AS current_version_number
                FROM contract_reviews r
                LEFT JOIN contract_review_versions v ON v.id = r.current_version_id
                WHERE r.id = ?
                """,
                (review_id,),
            ).fetchone()
        if row is None:
            raise ContractReviewNotFoundError(review_id)
        return self._review_from_row(row)

    def list(self, case_id: str) -> List[ContractReview]:
        self.require_case(case_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*, v.version_number AS current_version_number
                FROM contract_reviews r
                LEFT JOIN contract_review_versions v ON v.id = r.current_version_id
                WHERE r.case_id = ?
                ORDER BY r.updated_at DESC, r.id DESC
                """,
                (case_id,),
            ).fetchall()
        return [self._review_from_row(row) for row in rows]

    def detail(self, review_id: str) -> ContractReviewDetail:
        review = self.get(review_id)
        with self.connect() as connection:
            versions = [
                self._version_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM contract_review_versions
                    WHERE review_id = ? ORDER BY version_number DESC
                    """,
                    (review_id,),
                ).fetchall()
            ]
            current = None
            for version in versions:
                if version.id == review.current_version_id:
                    current = version
                    break
        sources = review.current_sources
        if current is not None:
            issues = validate_version(review, current)
        else:
            issues = []
        return ContractReviewDetail(
            review=review,
            sources=sources,
            current_version=current,
            versions=versions,
            coverage={},
            issues=issues,
        )

    def append_generated_version(
        self,
        review_id: str,
        *,
        body: str,
        risks: List[Dict[str, Any]],
        citations: Dict[str, LawSource],
        provider: Optional[str] = None,
        requested_provider: Optional[str] = None,
        actual_model: Optional[str] = None,
        task_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        sources: Optional[List[LawSource]] = None,
    ) -> ContractReviewVersion:
        review = self.get(review_id)
        if sources is not None:
            self._set_current_sources(review.case_id, review_id, sources)
        with self.connect() as connection:
            new_number = (review.current_version_number or 0) + 1
            now = utc_now()
            version_id = new_id()
            connection.execute(
                """
                INSERT INTO contract_review_versions(
                    id, review_id, version_number, status, body, risks, citations,
                    base_version_id, task_id, attempt_id, generator_provider,
                    requested_provider, actual_model, coverage, saved_at, confirmed_at
                ) VALUES (?, ?, ?, 'draft', ?, ?, ?, NULL, ?, ?, ?, ?, ?, '{}', ?, NULL)
                """,
                (
                    version_id,
                    review_id,
                    new_number,
                    body or "",
                    _dumps(risks or []),
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
                UPDATE contract_reviews
                SET current_version_id = ?, current_version_number = ?,
                    latest_task_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (version_id, new_number, task_id, now, review_id),
            )
        return self._fetch_version(version_id)

    def save_manual_version(
        self, review_id: str, *, body: str, base_version_id: Optional[str] = None
    ) -> ContractReviewVersion:
        review = self.get(review_id)
        with self.connect() as connection:
            new_number = (review.current_version_number or 0) + 1
            now = utc_now()
            version_id = new_id()
            # 继承上一个版本的 citations 作为起点
            parent_citations = {}
            parent_risks = []
            if review.current_version_id:
                parent_row = connection.execute(
                    "SELECT citations, risks FROM contract_review_versions WHERE id = ?",
                    (review.current_version_id,),
                ).fetchone()
                if parent_row is not None:
                    parent_citations = _loads(parent_row["citations"], {})
                    parent_risks = _loads(parent_row["risks"], [])
            status = "edited"
            connection.execute(
                """
                INSERT INTO contract_review_versions(
                    id, review_id, version_number, status, body, risks, citations,
                    base_version_id, task_id, attempt_id, generator_provider,
                    requested_provider, actual_model, coverage, saved_at, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, '{}', ?, NULL)
                """,
                (
                    version_id,
                    review_id,
                    new_number,
                    status,
                    body or "",
                    _dumps(parent_risks),
                    _dumps(parent_citations),
                    base_version_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE contract_reviews
                SET current_version_id = ?, current_version_number = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (version_id, new_number, now, review_id),
            )
        return self._fetch_version(version_id)

    def update_sources(
        self,
        review_id: str,
        *,
        add: List[SourceReference],
        remove: List[str],
        confirm_remove: bool = False,
    ) -> ContractReviewDetail:
        """当前 sources 是法条检索结果（由生成任务自动填充）；手动 remove 后可后续人工补 add。"""
        detail = self.detail(review_id)
        current = {source.handle: source for source in detail.sources}
        for handle in remove:
            current.pop(handle, None)
        added_sources: List[LawSource] = []
        with self.connect() as connection:
            # add 目前只支持传入 LawSource 对象（sidecar 填充），
            # workbench 用户侧不手动构造法条 SourceReference，先保留此分支
            for ref in add:
                if isinstance(ref, LawSource):
                    current[ref.handle] = ref
        self._set_current_sources(detail.review.case_id, review_id, list(current.values()))
        return self.detail(review_id)

    def confirm(self, review_id: str) -> ContractReviewDetail:
        detail = self.detail(review_id)
        version = detail.current_version
        if version is None:
            raise ContractReviewValidationError("当前没有可确认的版本")
        issues = validate_version(detail.review, version)
        if issues:
            raise ContractReviewConfirmationError(issues)
        warnings = check_citations(version.body, [
            {"law_name": getattr(s, "title", ""), "article": getattr(s, "article", None)}
            for s in version.citations.values()
        ])
        with self.connect() as connection:
            now = utc_now()
            connection.execute(
                """
                UPDATE contract_review_versions
                SET status = 'confirmed', confirmed_at = ?
                WHERE id = ?
                """,
                (now, version.id),
            )
        return self.detail(review_id)

    def resolve_material_text(self, material_id: str, case_id: str) -> str:
        """把已解析的所有段落拼接成合同全文；ReviewHandler 直接调用。"""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT m.id FROM materials m
                WHERE m.id = ? AND m.case_id = ?
                """,
                (material_id, case_id),
            ).fetchone()
            if row is None:
                raise ContractReviewValidationError("材料不存在或不属于当前案件")
            segments = connection.execute(
                """
                SELECT seg.text FROM material_segments seg
                JOIN material_parse_versions v ON v.id = seg.parse_version_id
                WHERE v.material_id = ? AND seg.status IN ('ok', 'partial')
                  AND v.status IN ('success', 'partial')
                ORDER BY seg.sequence, seg.id
                """,
                (material_id,),
            ).fetchall()
        return "\n".join((row["text"] or "").strip() for row in segments if (row["text"] or "").strip())

    def _set_current_sources(
        self, case_id: str, review_id: str, sources: List[LawSource]
    ) -> None:
        ordered = sorted(
            sources, key=lambda s: (s.sequence or 0, s.handle)
        )
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE contract_reviews
                SET current_sources = ?, updated_at = ?
                WHERE id = ?
                """,
                (_dumps([s.model_dump() for s in ordered]), utc_now(), review_id),
            )

    def _fetch_version(self, version_id: str) -> ContractReviewVersion:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM contract_review_versions WHERE id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise ContractReviewNotFoundError(version_id)
        return self._version_from_row(row)


class SourceReference(BaseModel):
    """手动 add 来源占位；合同审查生成后法条来源由 sidecar 自动填充 LawSource，
    workbench 用户侧暂不暴露手动 add 法条的 API。"""
    handle: str
    kind: Literal["law"] = "law"


def validate_version(
    review: ContractReview, version: ContractReviewVersion
) -> List[CitationIssue]:
    issues: List[CitationIssue] = []
    body = version.body or ""
    handles_in_body = {match.group(1) for match in CITATION_PATTERN.finditer(body)}
    if not body.strip():
        issues.append(CitationIssue(code="empty_review", message="合同审查报告正文为空"))
        return issues
    if not handles_in_body:
        issues.append(CitationIssue(
            code="no_citation",
            message="报告未引用任何法条来源（[[handle]] 形式），请补充法条引用后再确认",
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


class ReviewHandler:
    def __init__(
        self,
        reviews: ContractReviewRepository,
        tasks: TaskRepository,
        gateway,
    ) -> None:
        self.reviews = reviews
        self.tasks = tasks
        self.gateway = gateway

    def prepare(self, review_id: str) -> Dict[str, Any]:
        detail = self.reviews.detail(review_id)
        review = detail.review
        self.reviews.require_case(review.case_id, writable=True)
        segment_text = self.reviews.resolve_material_text(
            review.material_id, review.case_id
        )
        if not segment_text.strip():
            raise ContractReviewValidationError("材料尚未解析完成或无可用文本")
        return {
            "review_id": review.id,
            "material_text": segment_text,
            "contract_type": review.contract_type,
            "focus_points": review.focus_points,
            "base_version_id": review.current_version_id,
        }

    def normalize_retry(self, scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if scope is not None:
            raise ContractReviewValidationError("合同审查重试沿用原快照，暂不支持修改输入")
        return None

    async def __call__(
        self, context, input_snapshot: Dict[str, Any]
    ) -> Any:
        review_id = str(input_snapshot.get("review_id") or "")
        detail = self.reviews.detail(review_id)
        task = self.tasks.get(context.task_id)
        if task.case_id != detail.review.case_id:
            raise ContractReviewValidationError("合同审查与任务不属于同一案件")
        material_text = str(input_snapshot.get("material_text") or "").strip()
        if not material_text:
            raise ContractReviewValidationError("合同文本为空")

        await context.report(
            current_stage="正在审查合同",
            progress_detail="已冻结合同文本、合同类型与关注点，等待法条检索与模型返回",
        )

        response = await self.gateway.generate_review({
            "material_text": material_text[:120000],
            "contract_type": str(input_snapshot.get("contract_type") or ""),
            "focus_points": str(input_snapshot.get("focus_points") or ""),
            "top_k": 6,
            "quality": False,
        })

        report = str(response.get("report") or "").strip()
        risks = response.get("risks") or []
        raw_citations = response.get("citations") or {}
        if not report:
            raise ContractReviewValidationError("合同审查返回了空报告")

        await context.report(
            current_stage="正在写入合同审查版本",
            progress_detail=f"已获取 {len(risks)} 条结构化风险，{len(raw_citations)} 个法条来源",
        )

        # 把 raw_citations 转为 SummarySource 兼容结构
        citations: Dict[str, LawSource] = {}
        law_sources: List[LawSource] = []
        for handle, item in raw_citations.items():
            source = LawSource(**{
                **item,
                "source_id": item.get("source_id") or handle,
                "case_id": detail.review.case_id,
            })
            citations[handle] = source
            law_sources.append(source)

        version = self.reviews.append_generated_version(
            review_id,
            body=report,
            risks=risks,
            citations=citations,
            provider="openai-compatible",
            actual_model=None,
            task_id=context.task_id,
            attempt_id=context.attempt_id,
            sources=law_sources,
        )
        from .tasks import TaskOutcome

        return TaskOutcome(
            current_stage="合同审查已生成",
            progress_detail=(
                f"已保存第 {version.version_number} 版，"
                f"共 {len(risks)} 条风险、{len(raw_citations)} 条法条来源"
            ),
            result={
                "review_id": review_id,
                "version_id": version.id,
                "version_number": version.version_number,
                "risk_count": len(risks),
                "citation_count": len(raw_citations),
            },
        )
