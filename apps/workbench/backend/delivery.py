"""FR-07 保存、导出与交付版本。

把 confirmed 状态的研究摘要版本渲染为 Markdown/DOCX 交付文件：
- 事实抽取（案号/日期/金额）与可追溯性校验复用版本冻结的 citations；
- 校验通过导出为正式交付（official），显式 keep_issues 时导出草稿（draft）；
- 每次导出生成新记录与新文件，导出后不可变。
"""

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .cases import utc_now
from .contract_reviews import ContractReviewVersion, LawSource, RiskItem
from .summaries import (
    CITATION_PATTERN,
    CitationIssue,
    SummarySource,
    SummaryVersion,
)
from .tasks import new_id


# 事实抽取正则与归一化逻辑复制自 MCP/law-search/core/delivery_gate.py
# （该模块内部依赖 core.contract_review，无法跨包导入）。
_CASE_RE = re.compile(r"[（(]\s*\d{4}\s*[）)]\s*[^\s，。；、,;]{1,60}?号")
_DATE_RE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*万元")

_FACT_PATTERNS = (
    ("case_number", _CASE_RE),
    ("date", _DATE_RE),
    ("amount", _AMOUNT_RE),
)

FACT_KIND_LABELS = {
    "case_number": "案号",
    "date": "日期",
    "amount": "金额",
}

FACT_KIND_FIELDS = {
    "case_number": "case_numbers",
    "date": "dates",
    "amount": "amounts",
}

STATUS_LABELS = {"official": "正式交付", "draft": "草稿（保留未核实问题）"}


def _norm_fact(value: str) -> str:
    return (
        value.replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
        .replace("\u3000", "")
    )


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "").replace("（", "(").replace("）", ")")


def _raw_span(line: str, value: str):
    """在原始行中定位归一化后的值；允许值内部夹带空白（如 PDF 抽取的案号）。"""
    flexible = re.compile(r"\s*".join(re.escape(ch) for ch in value))
    match = flexible.search(line)
    if match is not None:
        return match.start(), match.end()
    return 0, min(len(line), len(value))


def extract_facts(body: str) -> List[Dict[str, Any]]:
    """逐行抽取案号/日期/金额，位置记录行号与 ±10 字摘录。

    案号在压缩空白后的行文本上匹配（与 MCP/law-search delivery_gate 一致），
    日期与金额直接在原始行上匹配。
    """
    facts: List[Dict[str, Any]] = []
    for lineno, line in enumerate((body or "").splitlines(), start=1):
        compact = re.sub(r"\s+", "", line)
        for kind, pattern in _FACT_PATTERNS:
            source = compact if kind == "case_number" else line
            for match in pattern.finditer(source):
                value = _norm_fact(match.group(0))
                start, end = _raw_span(line, value)
                facts.append(
                    {
                        "line": lineno,
                        "kind": kind,
                        "value": value,
                        "excerpt": line[max(0, start - 10):min(len(line), end + 10)],
                    }
                )
    return facts


def summarize_facts(facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "case_numbers": [],
        "dates": [],
        "amounts": [],
        "positions": facts,
    }
    for fact in facts:
        field = FACT_KIND_FIELDS[fact["kind"]]
        if fact["value"] not in result[field]:
            result[field].append(fact["value"])
    return result


def check_traceability(
    body: str, citations: Dict[str, SummarySource]
) -> List[CitationIssue]:
    """导出事实必须出现在正文实际引用来源的冻结文本中。"""
    cited_handles = {match.group(1) for match in CITATION_PATTERN.finditer(body or "")}
    corpus = "\n".join(
        citations[handle].text for handle in sorted(cited_handles) if handle in citations
    )
    normalized_corpus = _norm_text(corpus)
    issues: List[CitationIssue] = []
    for fact in extract_facts(body):
        if _norm_text(fact["value"]) not in normalized_corpus:
            label = FACT_KIND_LABELS[fact["kind"]]
            issues.append(
                CitationIssue(
                    code="fact_unverified",
                    message=(
                        f"第 {fact['line']} 行的{label}“{fact['value']}”"
                        f"未在已引用来源中出现（…{fact['excerpt']}…）"
                    ),
                )
            )
    return issues


def _sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", value or "").strip("._")
    return cleaned[:120] or "export"


def download_filename(delivery: "DeliveryVersion") -> str:
    ext = "md" if delivery.format == "markdown" else "docx"
    base = _sanitize_filename(delivery.summary_title)
    kind_tag = "review" if delivery.kind == "contract_review" else "summary"
    return f"{base}_v{delivery.version_number}_{kind_tag}_{delivery.format}_{delivery.status}.{ext}"


def render_markdown(
    summary_title: str,
    question: str,
    version: SummaryVersion,
    status: str,
    issues: List[CitationIssue],
    exported_at: str,
) -> str:
    lines: List[str] = []
    lines.append(f"# {summary_title}")
    lines.append("")
    lines.append("## 交付信息")
    lines.append("")
    lines.append(f"- 研究问题：{question or '（未填写）'}")
    lines.append(f"- 版本：v{version.version_number}（{STATUS_LABELS[status]}）")
    lines.append(f"- 生成器：{version.generator_provider or '手动编辑'}")
    if version.actual_model:
        lines.append(f"- 实际模型：{version.actual_model}")
    if version.task_id:
        lines.append(f"- 生成任务：{version.task_id}")
    if status == "official":
        lines.append("- 校验结论：引用与事实可追溯性校验通过")
    else:
        lines.append(f"- 校验结论：存在 {len(issues)} 个未核实问题，本文件为草稿")
        for issue in issues:
            lines.append(f"  - {issue.message}")
    lines.append(f"- 导出时间：{exported_at}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 正文")
    lines.append("")
    lines.append(version.body)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 来源清单")
    lines.append("")
    for handle in sorted(version.citations):
        source = version.citations[handle]
        lines.append(f"- **{handle}** {source.title}")
        lines.append(f"  - 类型：{source.kind}")
        if source.source_label:
            lines.append(f"  - 来源：{source.source_label}")
        if source.locator:
            lines.append(
                f"  - 定位：{json.dumps(source.locator, ensure_ascii=False)}"
            )
        lines.append(f"  - 抓取状态：{source.origin_status or '未知'}")
    lines.append("")
    return "\n".join(lines)


def render_docx(
    summary_title: str,
    question: str,
    version: SummaryVersion,
    status: str,
    issues: List[CitationIssue],
    exported_at: str,
) -> bytes:
    from docx import Document

    document = Document()
    document.add_heading(summary_title, level=0)

    document.add_heading("交付信息", level=1)
    document.add_paragraph(f"研究问题：{question or '（未填写）'}")
    document.add_paragraph(f"版本：v{version.version_number}（{STATUS_LABELS[status]}）")
    document.add_paragraph(f"生成器：{version.generator_provider or '手动编辑'}")
    if version.actual_model:
        document.add_paragraph(f"实际模型：{version.actual_model}")
    if version.task_id:
        document.add_paragraph(f"生成任务：{version.task_id}")
    if status == "official":
        document.add_paragraph("校验结论：引用与事实可追溯性校验通过")
    else:
        document.add_paragraph(
            f"校验结论：存在 {len(issues)} 个未核实问题，本文件为草稿"
        )
        for issue in issues:
            document.add_paragraph(issue.message, style="List Bullet 2")
    document.add_paragraph(f"导出时间：{exported_at}")

    document.add_heading("正文", level=1)
    for line in version.body.splitlines() or [""]:
        document.add_paragraph(line)

    document.add_heading("来源清单", level=1)
    for handle in sorted(version.citations):
        source = version.citations[handle]
        document.add_paragraph(f"{handle} {source.title}", style="List Bullet")
        details = [f"类型：{source.kind}"]
        if source.source_label:
            details.append(f"来源：{source.source_label}")
        if source.locator:
            details.append(
                f"定位：{json.dumps(source.locator, ensure_ascii=False)}"
            )
        details.append(f"抓取状态：{source.origin_status or '未知'}")
        for item in details:
            document.add_paragraph(item, style="List Bullet 2")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ---- 合同审查版本渲染 ----

RISK_LEVEL_LABELS = {"高危": "高危", "中等": "中等", "提示": "提示"}


def render_review_markdown(
    title: str,
    contract_type: str,
    focus_points: str,
    version: ContractReviewVersion,
    status: str,
    issues: List[CitationIssue],
    exported_at: str,
) -> str:
    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append("## 基本信息")
    lines.append(f"- 合同类型：{contract_type or '（未填写）'}")
    lines.append(f"- 关注重点：{focus_points or '（未填写）'}")
    lines.append(f"- 版本：v{version.version_number}（{STATUS_LABELS[status]}）")
    lines.append(f"- 生成器：{version.generator_provider or '手动编辑'}")
    if version.actual_model:
        lines.append(f"- 实际模型：{version.actual_model}")
    lines.append(f"- 导出时间：{exported_at}")
    lines.append("")

    lines.append("## 风险清单")
    if version.risks:
        level_counts = {"高危": 0, "中等": 0, "提示": 0}
        for risk in version.risks:
            level_counts[risk.level] = level_counts.get(risk.level, 0) + 1
        summary_parts = []
        if level_counts["高危"]:
            summary_parts.append(f"高危 {level_counts['高危']}")
        if level_counts["中等"]:
            summary_parts.append(f"中等 {level_counts['中等']}")
        if level_counts["提示"]:
            summary_parts.append(f"提示 {level_counts['提示']}")
        lines.append(f"共发现 **{' / '.join(summary_parts)}** 条风险。")
        lines.append("")

        for risk in version.risks:
            level_label = RISK_LEVEL_LABELS.get(risk.level, risk.level)
            lines.append(f"### [{level_label}] {risk.excerpt or '（无条款引用）'}")
            if risk.risk_issue:
                lines.append(f"**风险：** {risk.risk_issue}")
            if risk.suggestion:
                lines.append(f"**建议：** {risk.suggestion}")
            if risk.legal_basis:
                lines.append(f"**依据：** {risk.legal_basis}")
            lines.append("")
    else:
        lines.append("_本版本未生成结构化风险清单，以下为完整审查报告正文。_")
        lines.append("")

    lines.append("## 审查报告")
    if version.body.strip():
        lines.append(version.body)
        lines.append("")
    else:
        lines.append("_（本版本尚未填写审查报告正文）_")
        lines.append("")

    if status == "draft" and issues:
        lines.append("## ⚠️ 未核实问题（草稿）")
        for issue in issues:
            handle = f"[{issue.handle}] " if issue.handle else ""
            lines.append(f"- {handle}{issue.message}")
        lines.append("")

    lines.append("## 法条来源")
    if version.citations:
        for handle in sorted(version.citations):
            src = version.citations[handle]
            law_name = getattr(src, "law_name", None) or getattr(src, "title", "")
            article = getattr(src, "article", None)
            law_type = getattr(src, "law_type", "")
            lines.append(f"- {handle} 《{law_name}》" + (f" 第{article}条" if article else "") + (f" [{law_type}]" if law_type else ""))
    else:
        lines.append("_本版本无法条来源记录_")
    lines.append("")

    return "\n".join(lines)


def render_review_docx(
    title: str,
    contract_type: str,
    focus_points: str,
    version: ContractReviewVersion,
    status: str,
    issues: List[CitationIssue],
    exported_at: str,
) -> bytes:
    from docx import Document  # type: ignore

    document = Document()
    document.add_heading(title, level=0)

    document.add_heading("基本信息", level=1)
    info_lines = [
        f"合同类型：{contract_type or '（未填写）'}",
        f"关注重点：{focus_points or '（未填写）'}",
        f"版本：v{version.version_number}（{STATUS_LABELS[status]}）",
        f"生成器：{version.generator_provider or '手动编辑'}",
    ]
    if version.actual_model:
        info_lines.append(f"实际模型：{version.actual_model}")
    info_lines.append(f"导出时间：{exported_at}")
    for line in info_lines:
        document.add_paragraph(line)

    document.add_heading("风险清单", level=1)
    if version.risks:
        level_counts = {"高危": 0, "中等": 0, "提示": 0}
        for risk in version.risks:
            level_counts[risk.level] = level_counts.get(risk.level, 0) + 1
        summary_parts = []
        if level_counts["高危"]:
            summary_parts.append(f"高危 {level_counts['高危']}")
        if level_counts["中等"]:
            summary_parts.append(f"中等 {level_counts['中等']}")
        if level_counts["提示"]:
            summary_parts.append(f"提示 {level_counts['提示']}")
        document.add_paragraph(f"共发现 {' / '.join(summary_parts)} 条风险。")

        for risk in version.risks:
            document.add_heading(
                f"[{RISK_LEVEL_LABELS.get(risk.level, risk.level)}] {risk.excerpt or '（无条款引用）'}",
                level=2,
            )
            if risk.risk_issue:
                document.add_paragraph(f"风险：{risk.risk_issue}")
            if risk.suggestion:
                document.add_paragraph(f"建议：{risk.suggestion}")
            if risk.legal_basis:
                document.add_paragraph(f"依据：{risk.legal_basis}")
    else:
        document.add_paragraph("本版本未生成结构化风险清单。")

    document.add_heading("审查报告", level=1)
    if version.body.strip():
        for para in version.body.split("\n\n"):
            cleaned = para.strip()
            if cleaned:
                document.add_paragraph(cleaned)
    else:
        document.add_paragraph("（本版本尚未填写审查报告正文）")

    if status == "draft" and issues:
        document.add_heading("未核实问题（草稿）", level=1)
        for issue in issues:
            handle = f"[{issue.handle}] " if issue.handle else ""
            document.add_paragraph(f"{handle}{issue.message}", style="List Bullet")

    document.add_heading("法条来源", level=1)
    if version.citations:
        for handle in sorted(version.citations):
            src = version.citations[handle]
            law_name = getattr(src, "law_name", None) or getattr(src, "title", "")
            article = getattr(src, "article", None)
            law_type = getattr(src, "law_type", "")
            suffix = f" 第{article}条" if article else ""
            type_suffix = f" [{law_type}]" if law_type else ""
            document.add_paragraph(
                f"{handle} 《{law_name}》{suffix}{type_suffix}",
                style="List Bullet",
            )
    else:
        document.add_paragraph("本版本无法条来源记录")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class ExportCreate(BaseModel):
    format: Literal["markdown", "docx"]
    keep_issues: bool = False


class DeliveryVersion(BaseModel):
    id: str
    case_id: str
    summary_id: Optional[str] = None
    review_id: Optional[str] = None
    kind: Literal["summary", "contract_review"] = "summary"
    version_id: str
    summary_title: str
    version_number: int
    format: Literal["markdown", "docx"]
    status: Literal["official", "draft"]
    storage_key: str = Field(exclude=True)
    sha256: str
    size_bytes: int
    facts: Dict[str, Any] = Field(default_factory=dict)
    issues: List[CitationIssue] = Field(default_factory=list)
    generator_provider: Optional[str] = None
    actual_model: Optional[str] = None
    task_id: Optional[str] = None
    exported_at: str


class DeliveryList(BaseModel):
    items: List[DeliveryVersion]
    total: int


class DeliveryNotFoundError(Exception):
    pass


class DeliveryStorage:
    def __init__(self, data_dir: Path) -> None:
        self.files_root = (data_dir / "files").resolve()

    def save(self, delivery_id: str, fmt: str, content: bytes) -> Dict[str, Any]:
        ext = "md" if fmt == "markdown" else "docx"
        storage_key = f"deliveries/{delivery_id}.{ext}"
        final_path = self.files_root / storage_key
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(content)
        return {
            "storage_key": storage_key,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }

    def path_for(self, storage_key: str) -> Path:
        path = (self.files_root / storage_key).resolve(strict=True)
        path.relative_to(self.files_root)
        return path


class DeliveryRepository:
    def __init__(self, case_repository) -> None:
        self.case_repository = case_repository

    def connect(self):
        return self.case_repository.connect()

    @staticmethod
    def _from_row(row) -> DeliveryVersion:
        values = dict(row)
        values["facts"] = json.loads(values["facts"] or "{}")
        values["issues"] = [
            CitationIssue(**item) for item in json.loads(values["issues"] or "[]")
        ]
        return DeliveryVersion(**values)

    def create(
        self,
        delivery_id: str,
        case_id: str,
        summary_id: Optional[str],
        review_id: Optional[str],
        kind: str,
        version_id: str,
        summary_title: str,
        version_number: int,
        fmt: str,
        status: str,
        storage: Dict[str, Any],
        facts: Dict[str, Any],
        issues: List[CitationIssue],
        exported_at: str,
        generator_provider: Optional[str] = None,
        actual_model: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> DeliveryVersion:
        record = {
            "id": delivery_id,
            "case_id": case_id,
            "summary_id": summary_id,
            "review_id": review_id,
            "kind": kind,
            "version_id": version_id,
            "summary_title": summary_title,
            "version_number": version_number,
            "format": fmt,
            "status": status,
            "storage_key": storage["storage_key"],
            "sha256": storage["sha256"],
            "size_bytes": storage["size_bytes"],
            "facts": json.dumps(facts, ensure_ascii=False),
            "issues": json.dumps(
                [issue.model_dump() for issue in issues], ensure_ascii=False
            ),
            "generator_provider": generator_provider,
            "actual_model": actual_model,
            "task_id": task_id,
            "exported_at": exported_at,
        }
        columns = ", ".join(record.keys())
        placeholders = ", ".join("?" for _ in record)
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO delivery_versions ({columns}) VALUES ({placeholders})",
                tuple(record.values()),
            )
        return DeliveryVersion(
            id=delivery_id,
            case_id=case_id,
            summary_id=summary_id,
            review_id=review_id,
            kind=kind,
            version_id=version_id,
            summary_title=summary_title,
            version_number=version_number,
            format=fmt,
            status=status,
            storage_key=storage["storage_key"],
            sha256=storage["sha256"],
            size_bytes=storage["size_bytes"],
            facts=facts,
            issues=issues,
            generator_provider=generator_provider,
            actual_model=actual_model,
            task_id=task_id,
            exported_at=exported_at,
        )

    def list_by_case(self, case_id: str) -> List[DeliveryVersion]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delivery_versions
                WHERE case_id = ?
                ORDER BY exported_at DESC, id DESC
                """,
                (case_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, delivery_id: str) -> DeliveryVersion:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_versions WHERE id = ?", (delivery_id,)
            ).fetchone()
        if row is None:
            raise DeliveryNotFoundError(delivery_id)
        return self._from_row(row)
