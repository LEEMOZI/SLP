"""合同审查生成器：合同文本 → 法条检索 → build_review_prompt → LLM → 结构化风险。

注意：工作台 venv 不 import MCP law-search 的 Chromadb / LM Studio proxy。
build_review_prompt / build_review_query 从 MCP law-search/core/contract_review.py
纯函数复制（无外部依赖），retrieval_search 复用 sidecar 已注册的检索器。
"""

import json
import re
from typing import Any, Dict, List, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from .retrieval import search as retrieval_search
from .summarizer import SummarySettings


# ---- build_review_prompt / build_review_query 复制自 MCP law-search ----

_QUERY_HEAD_CHARS = 400
_ARTICLE_NO_RE = re.compile(r"第[0-9一二三四五六七八九十百千]+条")


def build_review_query(
    contract_text: str, contract_type: str = "", focus_points: str = ""
) -> str:
    parts: List[str] = []
    if (contract_type or "").strip():
        parts.append(f"{contract_type.strip()}合同 相关法律规定")
    fp = (focus_points or "").strip()
    if fp:
        parts.append(_ARTICLE_NO_RE.sub(" ", fp[:300]))
    if contract_text:
        parts.append(_ARTICLE_NO_RE.sub(" ", contract_text[:_QUERY_HEAD_CHARS]))
    return " ".join(p.strip() for p in parts if p.strip()).strip()


def build_review_prompt(
    contract_text: str,
    *,
    contract_type: str = "",
    focus_points: str = "",
    truncated: bool = False,
    evidence_context: str = "",
) -> str:
    """构建合同审查 prompt（复制自 MCP law-search/core/contract_review.py）。"""
    type_line = contract_type.strip() or "（未指定，请根据文本判断）"
    focus_line = focus_points.strip() or "（未指定，请全面审查）"
    trunc_note = (
        "\n注意：合同原文过长已被截断，仅审查前部分内容，报告中须说明此限制。"
        if truncated else ""
    )
    evidence_block = (
        evidence_context.strip()
        or "（检索未找到可用法条，请在报告中注明：本次审查未经知识库法条校验）"
    )
    return "\n".join([
        "你是一名资深中国执业律师，专长合同审查。请对以下合同进行全面审查，输出风险清单报告。",
        "",
        "【合同类型】",
        type_line,
        "",
        "【重点关注】",
        focus_line,
        "",
        "【合同全文】",
        contract_text,
        f"{trunc_note}",
        "",
        "【可用法律依据】（来自本地法律知识库检索，只可引用其中确实支持的条文）",
        evidence_block,
        "",
        "【报告结构要求】",
        "一、合同概况：合同类型、当事人、核心交易内容（3 行以内）",
        "二、风险清单：按 高危 / 中等 / 提示 三级分级。每条风险包含：",
        "    1. 风险条款（引用原文位置或原文摘要）",
        "    2. 风险说明（对我方的具体不利后果）",
        "    3. 修改建议（给出可直接使用的替代条款文本）",
        "    4. 法律依据（《法名》第X条，仅限【可用法律依据】中的条文）",
        "三、缺失条款提示：常见必要条款缺失清单（如违约责任、争议解决、解除条件等）",
        "四、总体结论：是否建议签署（可签 / 修改后可签 / 建议不签）及核心理由（3 行以内）",
        "",
        "【写作要求】",
        "1. 严格使用简体中文；法条引用必须采用《法名》第X条格式，且仅限【可用法律依据】中出现的条文，不得虚构。",
        "2. 风险判断须基于合同原文，不得臆测未出现的条款内容。",
        "3. 若【可用法律依据】中没有能支持某项风险判断的条文，该条的“法律依据”写“（本次检索证据未覆盖，建议人工核对相关法律规定）”，不得编造。",
        "4. 必须完整输出四个部分，尤其是“四、总体结论”不得省略。",
        "5. 直接输出报告正文，不要输出任何解释、前言或后记。",
    ])


# ---- 风险解析 ----

RISK_LEVELS = ("高危", "中等", "提示")
RISK_LEVEL_KEYWORDS = {"高危", "高风险", "中等风险", "中等", "提示风险", "提示"}


def parse_risks_from_body(body: str) -> List[Dict[str, Any]]:
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
        if any(keyword in line for keyword in RISK_LEVEL_KEYWORDS):
            _finalize()
            for level in RISK_LEVELS:
                if level in line:
                    current_level = level
                    break
            m = re.search(r"风险条款[：:]\s*(.+?)(?:[；;]\s*风险说明|$)", line)
            if m and current_level:
                current_risk = {"level": current_level, "excerpt": m.group(1).strip()}
            continue
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
        if current_level and current_risk is None and re.match(r"^[-*]\s*(.+)", line):
            excerpt = re.match(r"^[-*]\s*(.+)", line).group(1).strip()
            current_risk = {"level": current_level, "excerpt": excerpt}
            continue

    _finalize()
    for risk in risks:
        risk.setdefault("risk_issue", "")
        risk.setdefault("suggestion", "")
        risk.setdefault("legal_basis", "")
    return risks


# ---- 检索 → evidence_pack 构造 ----

def build_evidence_context(retrieval_result: Dict[str, Any], max_items: int = 12) -> str:
    """把 law-search 检索结果格式化为 prompt 的【可用法律依据】段落。"""
    items = []
    seen_laws = set()
    for row in retrieval_result.get("items", [])[:max_items]:
        title = str(row.get("title") or row.get("name") or "").strip()
        # 尝试从 title 提取法名，fallback 用 title 整体
        law_name = ""
        m = re.search(r"《([^《》]{2,40})》", title)
        if m:
            law_name = m.group(1).strip()
        if not law_name and title:
            law_name = title.strip()[:40]
        article = ""
        text = str(row.get("content") or row.get("excerpt") or "").strip()
        m2 = re.search(r"第\s*([0-9一二三四五六七八九十百千]+)\s*条", text[:300])
        if m2:
            article = f"第{m2.group(1)}条"
        sig = f"{law_name}|{article}"
        if sig in seen_laws or not law_name:
            continue
        seen_laws.add(sig)
        snippet = text[:200].replace("\n", " ").strip()
        items.append(f"• 《{law_name}》{article} — {snippet}")
    return "\n".join(items) if items else ""


def build_evidence_pack(retrieval_result: Dict[str, Any], max_items: int = 30) -> List[Dict[str, Any]]:
    """结构化 evidence_pack：给合同审查引用校验用（law_name / article / text）。"""
    pack: List[Dict[str, Any]] = []
    for row in retrieval_result.get("items", [])[:max_items]:
        title = str(row.get("title") or row.get("name") or "").strip()
        m = re.search(r"《([^《》]{2,40})》", title)
        law_name = m.group(1).strip() if m else title[:40]
        article = None
        text = str(row.get("content") or row.get("excerpt") or "").strip()
        m2 = re.search(r"第\s*([0-9一二三四五六七八九十百千]+)\s*条", text[:300])
        if m2:
            # 中文数字保留原字符串，合同审查 check_citations 用 _norm_article 转 int
            article_raw = m2.group(1).strip()
            try:
                article = int(article_raw) if article_raw.isdigit() else article_raw
            except ValueError:
                article = article_raw
        if not law_name:
            continue
        pack.append({
            "law_name": law_name,
            "article": article,
            "title": title,
            "text": text,
            "retrieval_score": row.get("score") or row.get("rank") or 0.0,
        })
    return pack


# ---- Pydantic ----

class ReviewRequest(BaseModel):
    material_text: str = Field(min_length=1, max_length=120000)
    contract_type: str = Field(default="", max_length=80)
    focus_points: str = Field(default="", max_length=1000)
    top_k: int = Field(default=6, ge=1, le=20)
    quality: bool = False

    @field_validator("material_text")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("合同文本不能为空")
        return value.strip()


class ReviewGenerationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ReviewGenerator:
    def __init__(
        self,
        settings: Optional[SummarySettings] = None,
        retriever: Any = None,
    ) -> None:
        self.settings = settings or SummarySettings.from_env()
        self.retriever = retriever

    def capability(self) -> Dict[str, Any]:
        return {
            "provider": self.settings.provider,
            "openai_configured": self.settings.openai_configured,
            "supports_quality": bool(self.settings.openai_configured),
            "limits": {"material_chars": 120000, "max_top_k": 20},
        }

    def generate(self, payload: ReviewRequest) -> Dict[str, Any]:
        if self.settings.provider != "openai-compatible" or not self.settings.openai_configured:
            raise ReviewGenerationError(
                "provider_unavailable",
                "合同审查需要配置 SLP_SUMMARY_OPENAI_BASE_URL / MODEL / API_KEY",
                502,
            )
        truncated = len(payload.material_text) > 80_000
        contract_text = payload.material_text[:80_000] if truncated else payload.material_text

        query = build_review_query(
            contract_text, payload.contract_type, payload.focus_points
        )
        try:
            retrieval_result = retrieval_search(
                "law", query, limit=payload.top_k, retriever=self.retriever
            )
        except Exception as exc:
            retrieval_result = {"items": [], "source": "law", "total": 0}
        evidence_context = build_evidence_context(retrieval_result)
        evidence_pack = build_evidence_pack(retrieval_result)

        prompt = build_review_prompt(
            contract_text,
            contract_type=payload.contract_type,
            focus_points=payload.focus_points,
            truncated=truncated,
            evidence_context=evidence_context,
        )

        try:
            report = self._call_llm(prompt, max_tokens=8000)
        except ReviewGenerationError:
            raise
        except Exception as exc:
            raise ReviewGenerationError(
                "llm_failed",
                f"LLM 调用失败：{type(exc).__name__}",
                502,
            ) from exc

        risks = parse_risks_from_body(report or "")

        # 构造 citations（法条来源 → LawSource 兼容结构）
        citations: Dict[str, Dict[str, Any]] = {}
        for idx, item in enumerate(evidence_pack, start=1):
            handle = f"L{idx}"
            citations[handle] = {
                "handle": handle,
                "kind": "law",
                "source_id": handle,
                "case_id": "",  # 工作台侧会回填
                "title": item["law_name"],
                "text": item["text"][:2000],
                "locator": {"article": item["article"]},
                "source_label": f"{item['law_name']} {item['article']}",
                "origin_status": "completed",
                "law_name": item["law_name"],
                "article": item["article"],
                "law_type": "statute",
            }

        return {
            "ok": True,
            "report": report,
            "risks": risks,
            "citations": citations,
            "citation_count": len(citations),
            "evidence_count": len(evidence_pack),
            "query": query,
            "truncated": truncated,
            "evidence_pack": evidence_pack,
        }

    def _call_llm(self, prompt: str, *, max_tokens: int = 4000) -> str:
        """直接用 httpx 调 OpenAI-compatible /chat/completions；从 summarizer._openai_compatible 复用。"""
        try:
            response = httpx.post(
                f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                json={
                    "model": self.settings.openai_model,
                    "messages": [
                        {"role": "system", "content": "你是一名资深中国执业律师，专长合同审查。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                },
                timeout=httpx.Timeout(self.settings.timeout_s, connect=5.0),
            )
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty model content")
            return content.strip()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            raise ReviewGenerationError(
                "llm_failed",
                f"LLM 请求或响应校验失败：{type(error).__name__}",
                502,
            ) from error
