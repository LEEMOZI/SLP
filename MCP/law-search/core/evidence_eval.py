# -*- coding: utf-8 -*-
"""core/evidence_eval.py — 诉讼证据评估支持模块（evaluate_evidence 工具）

职责：
    1. 评估 prompt 构建（三性分析 + 证明力排序 + 质证建议）
    2. 检索 query 构建（待证事实 + 证据类型 → 检索语句）

边界：
    - 只做纯函数；检索 / LLM / 引用校验由 pipeline 编排
    - 引用一致性校验复用 core.litigation_builder.check_citations
    - 评估对象是当事人提交的证据材料（书证/物证/证人证言/电子数据等），
      输出为诉讼策略参考，不构成对证据效力的司法认定
"""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger("law-search.evidence")

# 检索 query 用证据类型 + 待证事实，避免整段证据描述拖垮 embedding
_QUERY_FACTS_CHARS = 800
_QUERY_EVIDENCE_CHARS = 400


def build_eval_query(evidence_text: str, case_facts: str = "", claims: str = "") -> str:
    """证据评估检索 query：证据类型要点 + 待证事实 + 案情前段。"""
    parts: List[str] = []
    ev = (evidence_text or "").strip()
    if ev:
        # 取证据描述开头（通常含证据名称/类型）
        parts.append(ev[:_QUERY_EVIDENCE_CHARS])
    claims_txt = (claims or "").strip()
    if claims_txt:
        parts.append(claims_txt[:_QUERY_FACTS_CHARS // 2])
    facts = (case_facts or "").strip()
    if facts:
        parts.append(facts[:_QUERY_FACTS_CHARS])
    parts.append("民事诉讼证据规则 证明力")
    return " ".join(p.strip() for p in parts if p.strip()).strip()


def build_eval_prompt(
    evidence_text: str,
    *,
    case_facts: str = "",
    claims: str = "",
    evidence_context: str = "",
) -> str:
    """构建证据评估 prompt（三性 + 证明力 + 质证建议）。"""
    facts_block = case_facts.strip() or "（未提供，请根据证据描述合理推断）"
    claims_block = claims.strip() or "（未提供，请根据证据内容归纳可能的待证事实）"
    evidence_block = (
        evidence_context.strip()
        or "（检索未找到可用法条，请在报告中注明：本次评估未经知识库法条校验）"
    )

    return "\n".join([
        "你是一名资深中国执业律师，专长民事诉讼证据分析。请对以下证据材料逐项评估，输出证据评估报告。",
        "",
        "【案情背景】",
        facts_block,
        "",
        "【待证事实 / 诉讼主张】",
        claims_block,
        "",
        "【证据材料】（当事人提交，逐项列明名称、内容、来源）",
        evidence_text.strip(),
        "",
        "【可用法律依据】（来自本地法律知识库检索，只可引用其中确实支持的条文）",
        evidence_block,
        "",
        "【报告结构要求】",
        "一、证据清单梳理：编号列明各项证据及证明对象（每项 1 行）",
        "二、逐项三性分析：对每项证据分别分析 真实性 / 合法性 / 关联性，",
        "    给出 强 / 较强 / 一般 / 弱 判断及理由；引用法条须采用《法名》第X条格式，",
        "    仅限【可用法律依据】中出现的条文",
        "三、证明力排序：按证明力从强到弱排列，说明排序理由",
        "四、质证要点与补强建议：对方可能攻击的点 + 我方补强方案（如公证、",
        "    原件核对、证人出庭、鉴定申请等）",
        "五、总体评价：现有证据能否支撑待证事实（能 / 部分能 / 不能）及关键缺口",
        "",
        "【写作要求】",
        "1. 严格使用简体中文；法条引用仅限【可用法律依据】中出现的条文，不得虚构。",
        "2. 三性判断须基于证据描述本身，不得臆测未提供的信息；信息不足处以〔待补充〕标注。",
        "3. 必须完整输出五个部分，尤其是“五、总体评价”不得省略。",
        "4. 直接输出报告正文，不要输出任何解释、前言或后记。",
    ])
