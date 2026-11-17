"""诉讼文书生成器：案情 → 法条检索 → build_litigation_prompt → LLM → 结构化文书。

注意：工作台 venv 不 import MCP law-search 的 Chromadb / LM Studio proxy。
build_litigation_prompt 从 MCP law-search/core/litigation_builder.py
纯函数复制（无外部依赖），retrieval_search 复用 sidecar 已注册的检索器。
"""

import re
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from .retrieval import search as retrieval_search
from .summarizer import SummarySettings


# ---- 文书类型模板 ----

DOC_TYPE_LABELS = {
    "complaint": "民事起诉状",
    "defense": "民事答辩状",
    "representation": "代理词",
    "legal_opinion": "法律意见书",
}

DOC_TEMPLATES = {
    "complaint": {
        "role": "你是一名资深中国执业律师，专长民事诉讼。请根据以下案情起草一份民事起诉状。",
        "structure": [
            "一、标题：民事起诉状",
            "二、当事人信息：原告（姓名/名称、性别、出生日期、民族、住址、联系方式）、被告（同上）",
            "三、诉讼请求：逐条列明，金额精确到元，请求事项具体明确",
            "四、事实与理由：按时间顺序叙述案件事实，结合法律规定论证诉讼请求的合理性",
            "五、证据清单：列明证据名称、来源、证明目的",
            "六、落款：此致 XX 人民法院、具状人签名、日期",
        ],
    },
    "defense": {
        "role": "你是一名资深中国执业律师，专长民事诉讼。请根据以下案情起草一份民事答辩状。",
        "structure": [
            "一、标题：民事答辩状",
            "二、当事人信息：答辩人（被告）基本信息、被答辩人（原告）基本信息",
            "三、答辩意见：针对原告诉讼请求逐条回应，提出抗辩理由",
            "四、事实与理由：陈述己方事实，结合法律规定论证抗辩的合理性",
            "五、证据清单：列明证据名称、来源、证明目的",
            "六、落款：此致 XX 人民法院、答辩人签名、日期",
        ],
    },
    "representation": {
        "role": "你是一名资深中国执业律师，专长民事诉讼。请根据以下案情起草一份代理词。",
        "structure": [
            "一、标题：代理词",
            "二、前言：说明代理人身份、委托关系、出庭情况",
            "三、代理意见：分点论述，结合案件事实与法律规定",
            "四、事实与理由：详细论证，引用法条支撑每个论点",
            "五、证据分析：对关键证据进行分析和论证",
            "六、结论：总结代理意见，提出明确请求",
            "七、落款：代理人签名、日期",
        ],
    },
    "legal_opinion": {
        "role": "你是一名资深中国执业律师。请根据以下案情出具一份法律意见书。",
        "structure": [
            "一、标题：法律意见书",
            "二、引言：说明出具背景、委托事项、依据材料",
            "三、事实概要：客观陈述相关事实",
            "四、法律分析：逐项分析法律问题，引用法条和司法解释",
            "五、结论与建议：给出明确法律意见和可操作建议",
            "六、声明：说明意见书的局限性和使用范围",
            "七、落款：律师事务所名称、律师签名、日期",
        ],
    },
}


# ---- 检索查询构建 ----

_QUERY_HEAD_CHARS = 400
_ARTICLE_NO_RE = re.compile(r"第[0-9一二三四五六七八九十百千]+条")


def build_litigation_query(
    facts: str, claims: str = "", doc_type: str = ""
) -> str:
    """从案情和诉求构建法条检索查询。"""
    parts: List[str] = []
    type_label = DOC_TYPE_LABELS.get(doc_type, "")
    if type_label:
        parts.append(f"{type_label} 相关法律规定")
    if (claims or "").strip():
        parts.append(_ARTICLE_NO_RE.sub(" ", claims[:300]))
    if (facts or "").strip():
        parts.append(_ARTICLE_NO_RE.sub(" ", facts[:_QUERY_HEAD_CHARS]))
    return " ".join(p.strip() for p in parts if p.strip()).strip()


# ---- Prompt 构建 ----

def build_litigation_prompt(
    doc_type: str,
    *,
    title: str = "",
    parties: str = "",
    claims: str = "",
    facts: str = "",
    focus_points: str = "",
    truncated: bool = False,
    evidence_context: str = "",
) -> str:
    """构建诉讼文书起草 prompt。"""
    template = DOC_TEMPLATES.get(doc_type, DOC_TEMPLATES["complaint"])
    type_label = DOC_TYPE_LABELS.get(doc_type, "诉讼文书")
    title_line = title.strip() or type_label
    parties_line = parties.strip() or "（未提供，请根据案情推断）"
    claims_line = claims.strip() or "（未提供，请根据案情拟定）"
    facts_line = facts.strip() or "（未提供）"
    focus_line = focus_points.strip() or "（未指定，请全面论述）"
    trunc_note = (
        "\n注意：案情过长已被截断，仅基于前部分内容起草，文书中须说明此限制。"
        if truncated else ""
    )
    evidence_block = (
        evidence_context.strip()
        or "（检索未找到可用法条，请在文书中注明：本次起草未经知识库法条校验）"
    )
    structure_lines = "\n".join(f"  {item}" for item in template["structure"])

    return "\n".join([
        template["role"],
        "",
        f"【文书类型】{type_label}",
        "",
        f"【文书标题】{title_line}",
        "",
        "【当事人信息】",
        parties_line,
        "",
        "【诉讼请求/核心主张】",
        claims_line,
        "",
        "【案件事实】",
        facts_line,
        f"{trunc_note}",
        "",
        "【重点关注】",
        focus_line,
        "",
        "【可用法律依据】（来自本地法律知识库检索，只可引用其中确实支持的条文）",
        evidence_block,
        "",
        "【文书结构要求】",
        structure_lines,
        "",
        "【写作要求】",
        "1. 严格使用简体中文；法条引用必须采用《法名》第X条格式，且仅限【可用法律依据】中出现的条文，不得虚构。",
        "2. 事实叙述须基于【案件事实】，不得臆测未出现的事实。",
        "3. 若【可用法律依据】中没有能支持某项论述的条文，该处写"（本次检索证据未覆盖，建议人工核对相关法律规定）"，不得编造。",
        "4. 必须完整输出所有结构部分，不得省略。",
        "5. 直接输出文书正文，不要输出任何解释、前言或后记。",
    ])


# ---- 检索 → evidence 构造（复用 review_generator 的模式） ----

def build_evidence_context(retrieval_result: Dict[str, Any], max_items: int = 12) -> str:
    items = []
    seen_laws = set()
    for row in retrieval_result.get("items", [])[:max_items]:
        title = str(row.get("title") or row.get("name") or "").strip()
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
    pack: List[Dict[str, Any]] = []
    for row in retrieval_result.get("items", [])[:max_items]:
        title = str(row.get("title") or row.get("name") or "").strip()
        m = re.search(r"《([^《》]{2,40})》", title)
        law_name = m.group(1).strip() if m else title[:40]
        article = None
        text = str(row.get("content") or row.get("excerpt") or "").strip()
        m2 = re.search(r"第\s*([0-9一二三四五六七八九十百千]+)\s*条", text[:300])
        if m2:
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

class LitigationRequest(BaseModel):
    doc_type: str = Field(min_length=1, max_length=40)
    title: str = Field(default="", max_length=160)
    parties: str = Field(default="", max_length=2000)
    claims: str = Field(default="", max_length=10000)
    facts: str = Field(default="", max_length=80000)
    focus_points: str = Field(default="", max_length=2000)
    top_k: int = Field(default=8, ge=1, le=20)
    quality: bool = False

    @field_validator("doc_type")
    @classmethod
    def valid_doc_type(cls, value: str) -> str:
        if value not in DOC_TYPE_LABELS:
            raise ValueError(f"不支持的文书类型：{value}")
        return value

    @field_validator("facts")
    @classmethod
    def non_blank_facts(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("案件事实不能为空")
        return value.strip()


class LitigationGenerationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ---- Generator ----

class LitigationGenerator:
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
            "doc_types": list(DOC_TYPE_LABELS.keys()),
            "limits": {"facts_chars": 80000, "max_top_k": 20},
        }

    def generate(self, payload: LitigationRequest) -> Dict[str, Any]:
        if self.settings.provider != "openai-compatible" or not self.settings.openai_configured:
            raise LitigationGenerationError(
                "provider_unavailable",
                "诉讼文书生成需要配置 SLP_SUMMARY_OPENAI_BASE_URL / MODEL / API_KEY",
                502,
            )
        truncated = len(payload.facts) > 60_000
        facts = payload.facts[:60_000] if truncated else payload.facts

        query = build_litigation_query(facts, payload.claims, payload.doc_type)
        try:
            retrieval_result = retrieval_search(
                "law", query, limit=payload.top_k, retriever=self.retriever
            )
        except Exception:
            retrieval_result = {"items": [], "source": "law", "total": 0}
        evidence_context = build_evidence_context(retrieval_result)
        evidence_pack = build_evidence_pack(retrieval_result)

        prompt = build_litigation_prompt(
            payload.doc_type,
            title=payload.title,
            parties=payload.parties,
            claims=payload.claims,
            facts=facts,
            focus_points=payload.focus_points,
            truncated=truncated,
            evidence_context=evidence_context,
        )

        try:
            document = self._call_llm(prompt, max_tokens=8000)
        except LitigationGenerationError:
            raise
        except Exception as exc:
            raise LitigationGenerationError(
                "llm_failed",
                f"LLM 调用失败：{type(exc).__name__}",
                502,
            ) from exc

        # 构造 citations（法条来源 → LawSource 兼容结构）
        citations: Dict[str, Dict[str, Any]] = {}
        for idx, item in enumerate(evidence_pack, start=1):
            handle = f"L{idx}"
            citations[handle] = {
                "handle": handle,
                "kind": "law",
                "source_id": handle,
                "case_id": "",
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
            "document": document,
            "citations": citations,
            "citation_count": len(citations),
            "evidence_count": len(evidence_pack),
            "query": query,
            "truncated": truncated,
            "evidence_pack": evidence_pack,
        }

    def _call_llm(self, prompt: str, *, max_tokens: int = 8000) -> str:
        try:
            response = httpx.post(
                f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                json={
                    "model": self.settings.openai_model,
                    "messages": [
                        {"role": "system", "content": "你是一名资深中国执业律师，专长诉讼文书起草。"},
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
            raise LitigationGenerationError(
                "llm_failed",
                f"LLM 请求或响应校验失败：{type(error).__name__}",
                502,
            ) from error
