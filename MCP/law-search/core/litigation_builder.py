# -*- coding: utf-8 -*-
"""core/litigation_builder.py — 法律文书起草支持模块（write_litigation 工具）

职责：
    1. doc_type 规范化（中英文别名 → 标准键）
    2. 检索 query 构建（案情 + 诉求 → 检索语句）
    3. 文书 prompt 构建（四类模板结构 + 证据上下文 + 写作要求）
    4. 引用一致性校验（文书中引用的《法名》第X条 是否在检索证据内）

边界：
    - 只做纯函数；不加载模型、不连 Chroma（pipeline 负责检索与 LLM）
    - 不复用 answer_guard（问答覆盖率语义与文书起草不匹配），
      用 check_citations 做"引用来源一致性"轻量校验，warning 不阻断
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("law-search.litigation")

# --------------------------------------------------------------------------
# 文书类型
# --------------------------------------------------------------------------

DOC_TYPE_ALIASES: Dict[str, str] = {
    "complaint": "complaint",
    "起诉状": "complaint",
    "民事起诉状": "complaint",
    "defense": "defense",
    "答辩状": "defense",
    "民事答辩状": "defense",
    "representation": "representation",
    "代理词": "representation",
    "legal_opinion": "legal_opinion",
    "法律意见书": "legal_opinion",
    "意见书": "legal_opinion",
}

# 每类文书：显示名 / 输入诉求段的标签 / 结构要求 / 类型专属写作要求
DOC_TEMPLATES: Dict[str, Dict[str, str]] = {
    "complaint": {
        "label": "民事起诉状",
        "claims_label": "诉讼请求",
        "structure": (
            "标题「民事起诉状」→ 当事人信息（原告/被告：姓名或名称、住址、"
            "联系方式，未提供的以〔待补充〕标注）→ 诉讼请求（分项列明，"
            "金额或标的明确，末项通常为诉讼费承担）→ 事实与理由（按时间线"
            "陈述，每个关键主张后标注对应法条依据）→ 证据和证据来源 →"
            "落款（此致 ××人民法院；具状人；日期）"
        ),
        "notes": "诉讼请求必须具体、可执行；法律依据只支持原告主张的部分。",
    },
    "defense": {
        "label": "民事答辩状",
        "claims_label": "答辩要点",
        "structure": (
            "标题「民事答辩状」→ 答辩人信息（未提供的以〔待补充〕标注）→"
            "答辩意见（针对起诉状的各项诉讼请求逐项表态：认可/部分认可/"
            "不认可，分项列明）→ 事实与理由（按时间线陈述，每个抗辩主张后"
            "标注对应法条依据）→ 落款（此致 ××人民法院；答辩人；日期）"
        ),
        "notes": "答辩意见与诉讼请求逐项对应；抗辩主张须有法律依据支撑。",
    },
    "representation": {
        "label": "代理词",
        "claims_label": "代理意见要点",
        "structure": (
            "标题「代理词」→ 开头（审判长、审判员：受××委托，××律师担任"
            "其诉讼代理人，庭前查阅了本案材料…）→ 代理意见（分论点论述，"
            "每个论点结合事实与法条展开）→ 结语（综上，请求法庭支持…）→"
            "落款（代理人：××律师；日期）"
        ),
        "notes": "以论证为主：每个论点 = 事实 + 法条 + 结论；语气克制、面向法庭。",
    },
    "legal_opinion": {
        "label": "法律意见书",
        "claims_label": "咨询要点",
        "structure": (
            "标题「法律意见书」→ 致送对象 → 一、委托事项与背景 → 二、"
            "法律分析（分问题论述，每个结论后标注法条依据）→ 三、结论与"
            "建议（分项列明）→ 落款（××律师事务所；经办律师；日期）"
        ),
        "notes": "分析客观中立，既指出风险也给出可行建议；结论须可操作。",
    },
}


def normalize_doc_type(raw: str) -> Optional[str]:
    """doc_type 规范化：complaint/起诉状/民事起诉状… → 标准键；未知 → None。"""
    key = (raw or "").strip().lower()
    return DOC_TYPE_ALIASES.get(key) or DOC_TYPE_ALIASES.get((raw or "").strip())


# --------------------------------------------------------------------------
# 检索 query 构建
# --------------------------------------------------------------------------

# embedding/reranker 对超长文本效果下降；案情取前段足够定位法条
_MAX_FACTS_CHARS = 1500
_MAX_CLAIMS_CHARS = 600


def build_retrieval_query(case_facts: str, claims: str = "") -> str:
    """案情 + 诉求 → 检索语句（截断，避免超长 query 拖垮 embedding 召回）。"""
    parts: List[str] = []
    facts = (case_facts or "").strip()
    claims_txt = (claims or "").strip()
    if facts:
        parts.append(facts[:_MAX_FACTS_CHARS])
    if claims_txt:
        parts.append(claims_txt[:_MAX_CLAIMS_CHARS])
    return " ".join(parts).strip()


# --------------------------------------------------------------------------
# Prompt 构建
# --------------------------------------------------------------------------

def build_litigation_prompt(
    doc_type: str,
    case_facts: str,
    *,
    parties: str = "",
    claims: str = "",
    court: str = "",
    evidence_context: str = "",
) -> str:
    """构建文书起草 prompt。证据上下文来自 pipeline._phase4_context。"""
    tpl = DOC_TEMPLATES[doc_type]

    parties_block = parties.strip() or "（未提供，请按文书结构以〔待补充〕标注）"
    claims_block = claims.strip() or (
        f"（未提供，请根据案情事实合理归纳{tpl['claims_label']}）"
    )
    court_block = court.strip() or "〔待补充〕"
    evidence_block = evidence_context.strip() or "（检索未找到可用法条，请基于专业知识起草并在文末注明：本次起草未经知识库法条校验）"

    lines = [
        f"你是一名资深中国执业律师。请根据以下材料，起草一份{tpl['label']}。",
        "",
        "【当事人信息】",
        parties_block,
        "",
        "【受诉法院 / 致送对象】",
        court_block,
        "",
        "【案情事实】",
        (case_facts or "").strip(),
        "",
        f"【{tpl['claims_label']}】",
        claims_block,
        "",
        "【可用法律依据】（来自本地法律知识库检索，只可引用其中确实支持主张的条文）",
        evidence_block,
        "",
        "【文书结构要求】",
        tpl["structure"],
        "",
        "【写作要求】",
        "1. 严格使用简体中文法律文书语体；法条引用必须采用《法名》第X条格式，且仅限【可用法律依据】中出现的条文，不得虚构或引用依据之外的条文。",
        "2. 事实部分只基于【案情事实】，不得虚构事实或证据；信息不足处以〔待补充〕标注。",
        f"3. {tpl['notes']}",
        "4. 直接输出文书正文，不要输出任何解释、前言或后记。",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 引用一致性校验
# --------------------------------------------------------------------------

_CITE_LAW_RE = re.compile(r"《([^《》]{2,40})》")
_CITE_ART_RE = re.compile(r"第([0-9一二三四五六七八九十百千]+)条")

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(num_str: str) -> Optional[int]:
    """中文数字 → int（支持 1-999，如 二十三 → 23）；纯阿拉伯数字直接转。"""
    s = (num_str or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)

    def segment(seg: str) -> int:
        # seg 不含 "百"
        if "十" in seg:
            left, _, right = seg.partition("十")
            tens = _CN_DIGITS.get(left, 1) if left else 1
            ones = _CN_DIGITS.get(right, 0) if right else 0
            return tens * 10 + ones
        return _CN_DIGITS.get(seg, -1)

    try:
        if "百" in s:
            left, _, rest = s.partition("百")
            hundreds = _CN_DIGITS.get(left, -1)
            if hundreds < 0:
                return None
            return hundreds * 100 + (segment(rest) if rest else 0)
        return segment(s)
    except Exception:
        return None


def _norm_article(article: str) -> Optional[int]:
    """"第十条"/"10条"/"10" → 10。"""
    s = (article or "").strip().removeprefix("第").removesuffix("条").strip()
    return _cn_to_int(s)


def extract_cited_articles(text: str) -> List[Tuple[str, int]]:
    """从文书提取（《法名》, 条号 int）列表，按出现顺序去重。

    处理中文文书惯例：同一部法律连续引用只写一次书名号，
    如「《民法典》第十条、第一百四十三条」→ 两个条号都提取。
    """
    out: List[Tuple[str, int]] = []
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


def _law_matches(cite_law: str, known_law: str) -> bool:
    """法律名称匹配：精确相等或双向包含（处理简称 vs 全称，
    如《民法典》vs《中华人民共和国民法典》）。"""
    a, b = cite_law.strip(), known_law.strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def check_citations(
    document: str, evidence_pack: List[Dict[str, Any]]
) -> List[str]:
    """引用一致性校验：文书引用的法条 vs 检索证据。

    返回 warning 列表（不阻断）：
      - 引用了证据之外的法律（law_name 未命中）→ 提示人工复核来源
      - 法律命中但条号未命中 → 同样提示
    """
    if not (document or "").strip():
        return ["文书为空，未做引用校验。"]

    known: set = set()
    for ev in evidence_pack or []:
        law = (ev.get("law_name") or "").strip()
        num = _norm_article(str(ev.get("article") or ""))
        if law:
            known.add((law, num))  # num 可能 None：法律级命中
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
