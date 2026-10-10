# -*- coding: utf-8 -*-
"""
core.query_understanding
========================

Query Understanding（QU）层 — 纯规则实现、零外部依赖（只用 re / unicodedata）。

对用户提问做：
  1. normalize          — NFC / 半角全角 / 多余空白 / 中英文标点
  2. classify_intent    — 5 类：article_lookup / case_analysis / definition / procedural / general
  3. expand_synonyms    — 法律同义词扩展（替换 + 追加备选）
  4. normalize_articles — 把「公司法第三条」拆成 law=公司法, article=3；同时产出阿拉伯+中文数字双版本，
                          便于 retrieval_service 做 $or $contains 宽松匹配
  5. remove_stopwords   — 中文法律停用词剥离（不改 primary_query，仅在 expand queries 中用）
  6. generate_queries   — 主查询 + 最多 2 个备选扩展查询（同义版 + 法名+条号版）
  7. build_where_hints  — 把 QU 产出的 law_names/articles 转为 retrieval_service 可直接消费的
                          {article_numbers:[], law_keywords:[], source_categories:[]}
                          （同时保留原始 extract_article_number/detect_law_keywords 签名，保证兼容）

P1-1 引入 QU 的目的：从 Recall 上限决定层（"检索不到的东西不会出现在 prompt 里"）切下最大一刀。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

# ==========================================================================
# 法律同义词表（按「规范化核心词」→ 同义词集合 存储，命中时用核心词做替换）
# ==========================================================================

SYNONYM_GROUPS: Dict[str, Tuple[str, ...]] = {
    "出资不实": ("出资不实", "虚假出资", "出资瑕疵", "未足额出资", "出资不足", "未实缴",
                 "未按时出资", "缴纳出资不实"),
    "抽逃出资": ("抽逃出资", "抽回出资", "抽逃注册资本", "抽资"),
    "连带责任": ("连带责任", "连带赔偿责任", "连带清偿责任", "承担连带责任"),
    "股东": ("股东", "出资人", "投资者", "持股人"),
    "有限责任公司": ("有限责任公司", "有限公司", "有限责任公司的", "有限责任公司制"),
    "股份有限公司": ("股份有限公司", "股份公司"),
    "法定代表人": ("法定代表人", "法人", "法人代表"),
    "公司章程": ("公司章程", "章程", "公司的章程"),
    "公司法": ("公司法", "中华人民共和国公司法", "中国公司法"),
    "民法典": ("民法典", "中华人民共和国民法典", "民法通则", "民总", "民法总则"),
    "民事诉讼法": ("民事诉讼法", "民诉法", "民事诉讼程序法", "中华人民共和国民事诉讼法"),
    "劳动合同法": ("劳动合同法", "劳动法", "劳动合同", "中华人民共和国劳动合同法"),
    "破产法": ("破产法", "企业破产法", "破产程序"),
    "诉讼时效": ("诉讼时效", "时效", "诉讼时效期间", "除斥期间"),
    "违约金": ("违约金", "违约损失", "逾期违约金", "约定违约金"),
    "解除合同": ("解除合同", "合同解除", "撤销合同", "解除协议"),
    "合同无效": ("合同无效", "无效合同", "无效条款", "违反法律强制性规定"),
    "一人公司": ("一人公司", "一人有限责任公司", "一人有限公司"),
    "股权转让": ("股权转让", "股权变更", "股权转让协议", "受让股权"),
    "清算": ("清算", "清算程序", "破产清算", "解散清算"),
    "董事": ("董事", "执行董事", "董事会成员"),
    "高级管理人员": ("高级管理人员", "高管", "高级经理"),
    "监事": ("监事", "监事会成员", "执行监事"),
    "股权回购": ("股权回购", "回购股权", "公司回购"),
    "抵押权": ("抵押权", "抵押", "抵押担保", "最高额抵押"),
    "质权": ("质权", "质押", "质押担保"),
    "留置权": ("留置权", "留置"),
    "代位权": ("代位权", "代位求偿", "债权人代位"),
    "撤销权": ("撤销权", "债权人撤销", "撤销行为"),
    "分公司": ("分公司", "分支机构"),
    "子公司": ("子公司", "全资子公司", "控股子公司"),
    "公司解散": ("公司解散", "解散公司", "公司清算", "司法解散"),
    "股东会": ("股东会", "股东大会", "股东会议"),
    "出资加速到期": ("出资加速到期", "加速到期", "注册资本加速到期", "出资义务加速到期"),
    "法人人格否认": ("法人人格否认", "人格否认", "刺破公司面纱", "揭开公司面纱"),
}

# 倒排 index：「同义词 token → 规范化核心词」，查找 O(1)
_SYNONYM_INDEX: Dict[str, str] = {}
for _canonical, _variants in SYNONYM_GROUPS.items():
    for _v in _variants:
        _SYNONYM_INDEX.setdefault(_v, _canonical)

# 按长度降序，保证「出资不实」比「出资」先匹配，避免切片错误
_SYNONYM_TOKENS: Tuple[str, ...] = tuple(
    sorted(_SYNONYM_INDEX.keys(), key=len, reverse=True)
)


# ==========================================================================
# 停用词（中文法律问答常见无信息量词；仅用于备用扩展查询，不影响主查询）
# ==========================================================================

STOPWORDS = frozenset([
    "请", "请问", "我想", "帮我", "一下", "什么是", "什么叫", "是否", "是不是",
    "应该", "应当", "需要", "可以", "可能", "如果", "假如", "假设", "那么",
    "在这种情况下", "这种", "那种", "这些", "那些", "这个", "那个",
    "想知道", "咨询", "咨询一下", "想咨询", "了解", "想了解一下", "解答",
    "告诉", "解释", "解释一下", "说明", "说明一下", "给出", "回答",
    "关于", "对于", "针对", "涉及", "方面", "问题", "法律问题", "相关",
    "怎么", "如何", "为什么", "多少", "几", "呢", "吗", "吧", "啊",
    "了", "的", "和", "与", "及", "或", "等", "等等", "之类",
])


# ==========================================================================
# 法名快速识别表（与 retrieval_service.LAW_KEYWORDS_FOR_CONTAINS 对齐子集，
# 不直接 import 保持 QU 层独立性）
# ==========================================================================

LAW_NAME_CANONICAL: Dict[str, str] = {
    "公司法": "公司法",
    "中华人民共和国公司法": "公司法",
    "有限责任公司": "公司法",
    "有限公司": "公司法",
    "股份有限公司": "公司法",
    "股份公司": "公司法",
    "一人有限责任公司": "公司法",
    "一人公司": "公司法",
    "民法典": "民法典",
    "中华人民共和国民法典": "民法典",
    "民事诉讼法": "民事诉讼法",
    "中华人民共和国民事诉讼法": "民事诉讼法",
    "劳动合同法": "劳动合同法",
    "中华人民共和国劳动合同法": "劳动合同法",
    "劳动法": "劳动法",
    "企业破产法": "破产法",
    "破产法": "破产法",
    "刑法": "刑法",
    "刑事诉讼法": "刑事诉讼法",
    "行政诉讼法": "行政诉讼法",
    "行政复议法": "行政复议法",
    "证券法": "证券法",
    "反垄断法": "反垄断法",
    "反不正当竞争法": "反不正当竞争法",
    "消费者权益保护法": "消费者权益保护法",
    "保险法": "保险法",
    "票据法": "票据法",
    "个人独资企业法": "个人独资企业法",
    "合伙企业法": "合伙企业法",
    "外商投资法": "外商投资法",
    "继承法": "继承法",
    "婚姻法": "婚姻法",
    "收养法": "收养法",
    "著作权法": "著作权法",
    "专利法": "专利法",
    "商标法": "商标法",
    "法人人格否认": "公司法",
    "人格否认": "公司法",
    "刺破公司面纱": "公司法",
    "揭开公司面纱": "公司法",
    "劳动仲裁": "劳动争议调解仲裁法",
    "仲裁": "仲裁法",
    "合同": "民法典",
    "买卖合同": "民法典",
    "借款合同": "民法典",
    "租赁合同": "民法典",
    "担保": "民法典",
    "物权": "民法典",
    "侵权责任": "民法典",
    "婚姻": "民法典",
    "继承": "民法典",
    "收养": "民法典",
    "工伤": "工伤保险条例",
    "公司解散": "公司法",
    "司法解散": "公司法",
    "股权回购": "公司法",
    "股权转让": "公司法",
    "出资加速到期": "公司法",
    "抽逃出资": "公司法",
    "出资不实": "公司法",
    "虚假出资": "公司法",
    "连带责任": "公司法",
    "股东": "公司法",
    "董事": "公司法",
    "高管": "公司法",
    "高级管理人员": "公司法",
    "监事": "公司法",
    "忠实义务": "公司法",
    "勤勉义务": "公司法",
    "劳动合同解除": "劳动合同法",
    "劳动合同": "劳动合同法",
    "劳动仲裁": "劳动争议调解仲裁法",
    "劳动争议": "劳动争议调解仲裁法",
    "工伤": "工伤保险条例",
    "财产保全": "民事诉讼法",
    "证据保全": "民事诉讼法",
    "管辖权异议": "民事诉讼法",
    "管辖法院": "民事诉讼法",
    "起诉": "民事诉讼法",
    "上诉": "民事诉讼法",
    "申诉": "民事诉讼法",
    "再审": "民事诉讼法",
    "执行程序": "民事诉讼法",
    "立案": "民事诉讼法",
    "诉讼时效": "民法典",
    "代位权": "民法典",
    "撤销权": "民法典",
    "抵押权": "民法典",
    "质权": "民法典",
    "留置权": "民法典",
    "违约金": "民法典",
    "解除合同": "民法典",
    "合同无效": "民法典",
}

LAW_NAME_TOKENS: Tuple[str, ...] = tuple(
    sorted(LAW_NAME_CANONICAL.keys(), key=len, reverse=True)
)


# ==========================================================================
# 条号抽取与中⇄阿数字互转
# ==========================================================================

# 完整形态：「第 一~三百九十九 条/款/项」+ 简写「3条 / 第三款 / 第五项」
_ART_NUM = r"([0-9０-９零一二三四五六七八九十百千万两]{1,10})"
_ARTICLE_PATTERN_FULL = re.compile(
    rf"第?\s*{_ART_NUM}\s*(条|款|项|节|章)"
)

_CN_TO_INT = {
    "零": 0, "〇": 0, "○": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn_num_to_int(s: str) -> Optional[int]:
    """把中文数字（一~三百九十九 / 简写阿拉伯「１２３」全角）转为 int。
    非数字串返回 None。"""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    # 全角→半角
    s = "".join(
        chr(ord(c) - 0xFEE0) if "０" <= c <= "９" else c
        for c in s
    )
    # 纯阿拉伯数字，直接 int
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    # 中文数字（只处理 0~39999，够用：「第三百九十九条」=399）
    total = 0
    current = 0
    temp = 0  # 数字临时累计，遇单位乘
    for ch in s:
        if ch in _CN_TO_INT:
            temp = temp * 10 + _CN_TO_INT[ch] if temp == 0 or _CN_TO_INT[ch] == 0 else (temp or 1) * 10 + _CN_TO_INT[ch]
            # 更简洁写法：上面太别扭；重写为状态机
        elif ch in _UNIT:
            pass
        else:
            return None
    # 改走更可靠的算法（重写）：
    total = 0
    section = 0
    for ch in s:
        if ch in _CN_TO_INT:
            val = _CN_TO_INT[ch]
            # section 形如「三」在「三十」前 → section=3
            section = section * 10 + val if section > 0 or val != 0 else val
        elif ch in _UNIT:
            u = _UNIT[ch]
            if section == 0:
                section = 1
            if u == 10000:
                # 万是高阶，把前面的 total 也带进去
                total = (total + section) * u
                section = 0
            else:
                section = section * u if section > 0 else u
                total += section
                section = 0
        else:
            return None
    return total + section


def int_to_cn(n: int) -> str:
    """阿拉伯数字转中文数字（0~9999）。"""
    if n is None:
        return ""
    if n == 0:
        return "零"
    if n < 0:
        return ""
    digits = list("零一二三四五六七八九")
    units = ["", "十", "百", "千"]
    s = str(n)
    ln = len(s)
    out_chars: List[str] = []
    zero_pending = False
    for idx, ch in enumerate(s):
        v = int(ch)
        pos = ln - idx - 1
        if v == 0:
            zero_pending = True
            # 末尾不写零；万/个位单位也要有，但 "零千" 不行
            if pos == 0 and out_chars and out_chars[-1] == "零":
                out_chars.pop()
            continue
        if zero_pending:
            out_chars.append("零")
            zero_pending = False
        out_chars.append(digits[v])
        out_chars.append(units[pos])
    # 特殊：10-19 以「十」开头，不写「一十」
    joined = "".join(out_chars)
    if ln == 2 and joined.startswith("一十"):
        joined = joined[1:]
    return joined


# ==========================================================================
# Normalize
# ==========================================================================

_PUNCT_ENG_TO_CN = {",": "，", ";": "；", "!": "！", "?": "？", ":": "：",
                    "(": "（", ")": "）", "<": "《", ">": "》"}
_PUNCT_EXTRA_STRIP = "。.,、；;:!！?？<>《》()（）\"\'“”‘’"


def normalize_text(q: str) -> str:
    """NFC / 全角半角统一 / 多余空白压缩。"""
    if not q:
        return ""
    q = unicodedata.normalize("NFKC", q)
    # 把 ASCII 逗号/分号/问号/感叹号在中文语境中替换为中文标点
    out = []
    for c in q:
        if c in _PUNCT_ENG_TO_CN and out and any("\u4e00" <= x <= "\u9fff" for x in out[-3:]):
            out.append(_PUNCT_ENG_TO_CN[c])
        else:
            out.append(c)
    s = "".join(out)
    # 压缩空白
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ==========================================================================
# Synonym expansion
# ==========================================================================

def expand_synonyms(q: str) -> Tuple[str, List[Tuple[str, str]]]:
    """同义词替换 + 命中对列表。

    Returns:
        (replaced_query, [(original_token, canonical_token), ...])
        不会对没有命中的原串做任何改动，保证兼容。
    """
    if not q:
        return "", []
    hits: List[Tuple[str, str]] = []
    replaced = q
    # 长度优先
    for tok in _SYNONYM_TOKENS:
        if tok in replaced and tok != _SYNONYM_INDEX[tok]:
            canonical = _SYNONYM_INDEX[tok]
            replaced = replaced.replace(tok, canonical)
            hits.append((tok, canonical))
    return replaced, hits


# ==========================================================================
# 停用词：只在备用查询中使用，不改原 query
# ==========================================================================

def remove_stopwords(q: str) -> str:
    s = q or ""
    for w in sorted(STOPWORDS, key=len, reverse=True):
        if w in s:
            s = s.replace(w, " ")
    s = re.sub(r"\s+", " ", s).strip(" " + _PUNCT_EXTRA_STRIP)
    return s


# ==========================================================================
# 法名 / 条号 提取（双版本数字）
# ==========================================================================

def extract_law_names(q: str) -> List[str]:
    """从 query 中提取法名列表（去重、按在 query 中首次出现顺序返回）。"""
    if not q:
        return []
    seen: Dict[str, None] = {}
    for tok in LAW_NAME_TOKENS:
        if tok in q:
            seen.setdefault(LAW_NAME_CANONICAL[tok], None)
    return list(seen.keys())


def extract_articles_structured(q: str) -> List[Dict[str, Any]]:
    """比 retrieval_service.extract_article_number 更结构化：

    返回列表（一条 query 可能提到多个条/款/项）：
    [{"raw": "第3条", "kind": "条",
      "cn_num": "三", "ar_num": "3",
      "ar_int": 3, "cn_form": "第三条", "ar_form": "第3条"}, ...]
    """
    if not q:
        return []
    results: List[Dict[str, Any]] = []
    # 先用 full pattern 找「第X条」类，保证「第三款第五项」能分开
    for m in _ARTICLE_PATTERN_FULL.finditer(q):
        raw = m.group(0)
        num_s = m.group(1)
        kind = m.group(2)
        ar_int = cn_num_to_int(num_s)
        if ar_int is None:
            continue
        cn_n = int_to_cn(ar_int)
        results.append({
            "raw": raw,
            "kind": kind,
            "ar_int": ar_int,
            "ar_num": str(ar_int),
            "cn_num": cn_n,
            "cn_form": f"第{cn_n}{kind}",
            "ar_form": f"第{ar_int}{kind}",
        })
    return results


# ==========================================================================
# Intent 分类（5 类，基于关键词 + 结构）
# ==========================================================================

_INTENT_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("article_lookup", (
        "第几条", "第几款", "第几项", "第.*条", "第.*款", "第.*项",
        "多少条", "条文", "法条", "依据",
    )),
    ("definition", (
        "什么是", "什么叫", "定义", "含义", "概念", "指的是",
    )),
    ("case_analysis", (
        "怎么办", "责任", "承担", "赔偿", "可否", "能不能", "可以吗",
        "是否有效", "有效吗", "无效吗", "后果", "处罚", "构成", "认定",
        "义务", "权利", "风险", "赔偿责任", "违约金", "撤销", "解除",
    )),
    ("procedural", (
        "起诉流程", "上诉流程", "申诉流程", "再审程序", "仲裁程序",
        "执行程序", "立案程序", "管辖权异议", "管辖法院", "财产保全",
        "证据保全", "如何立案", "怎么起诉", "怎么申请",
    )),
]


def classify_intent(q: str) -> Tuple[str, List[str]]:
    """5 类意图：article_lookup / definition / case_analysis / procedural / general。"""
    if not q:
        return "general", []
    matched_rules: List[str] = []
    for name, pats in _INTENT_RULES:
        for p in pats:
            if ".*" in p:
                if re.search(p, q):
                    matched_rules.append(name)
                    break
            elif p in q:
                matched_rules.append(name)
                break
    # 有法名+条号 → article_lookup 绝对优先
    if extract_law_names(q) and any(r["kind"] == "条" for r in extract_articles_structured(q)):
        return "article_lookup", matched_rules or ["article_lookup"]
    if matched_rules:
        return matched_rules[0], matched_rules
    # 兜底：出现 1+ 个法律/公司核心词 + 有同义词暗示或单法名+1+实体/条号，默认算 case_analysis
    law_count = len(extract_law_names(q))
    syn_count = len(expand_synonyms(q)[1])
    article_count = len(extract_articles_structured(q))
    if law_count >= 1 and (syn_count >= 1 or article_count >= 1):
        return "case_analysis", ["fallback_case_analysis"]
    if law_count >= 2:
        return "case_analysis", ["fallback_multi_laws"]
    # 单法名 + 结尾有 "条件/流程/方式/情形/时效/范围/标准/后果/区别" 等 case 型词 → case_analysis
    if law_count == 1 and re.search(r"(条件|流程|方式|情形|时效|范围|标准|后果|区别|规定|要求|限制|程序|步骤)$", q.strip("。?？!！ ")):
        return "case_analysis", ["fallback_condition"]
    return "general", []


# ==========================================================================
# Query Expansion（主查询 + 备选）
# ==========================================================================

def generate_queries(q: str) -> List[str]:
    """返回最多 3 条查询（原主查询 + 两个变体），去重。"""
    norm = normalize_text(q)
    if not norm:
        return []

    primary = norm
    syn_replaced, syn_hits = expand_synonyms(norm)
    # 版本2：同义替换版
    v2 = syn_replaced if syn_hits and syn_replaced != primary else ""

    # 版本3：法名 + 条号 聚焦版（article_lookup 类）
    laws = extract_law_names(norm)
    arts = extract_articles_structured(norm)
    v3 = ""
    if laws and arts:
        # "公司法 第3条 第三条 股东出资不实"
        parts: List[str] = list(laws)
        for a in arts:
            parts.extend([a["ar_form"], a["cn_form"]])
        core_q = remove_stopwords(primary)
        if core_q and core_q != primary:
            parts.append(core_q)
        v3 = " ".join(p for p in parts if p)

    outs = [primary]
    for alt in (v2, v3):
        alt = alt.strip()
        if alt and alt not in outs and len(outs) < 3:
            outs.append(alt)
    # 兜底：如只有 primary，再补一个「去停用词版」，增加 recall
    if len(outs) < 2:
        stripped = remove_stopwords(primary)
        if stripped and stripped != outs[-1]:
            outs.append(stripped)
    return outs


# ==========================================================================
# Build where hints（给 retrieval_service 消费：article_numbers / law_keywords）
# ==========================================================================

def build_where_hints(q: str) -> Dict[str, List[str]]:
    """与旧 API extract_article_number + detect_law_keywords 兼容，
    但额外返回「阿拉伯/中文双版本条号 + 法名规范化列表 + source_category 优先级建议」。"""
    arts = extract_articles_structured(q)
    laws = extract_law_names(q)
    article_numbers: List[str] = []
    for a in arts:
        for form in (a["ar_form"], a["cn_form"]):
            if form and form not in article_numbers:
                article_numbers.append(form)
    return {
        "article_numbers": article_numbers,
        "law_keywords": laws,
        "raw_articles": arts,
    }


# ==========================================================================
# 主入口：understand() — 返回结构化 QU 结果
# ==========================================================================

def understand(query: str) -> Dict[str, Any]:
    """P1-1 QU 主入口。

    Returns:
        {
          "raw":                     原始 query,
          "normalized":              NFKC + 全半角,
          "intent":                  article_lookup/case_analysis/definition/procedural/general,
          "intent_hits":             命中的多意图,
          "law_names":               ["公司法", ...],
          "articles":                [{"raw": "第3条", "ar_int":3, "ar_form":"第3条", "cn_form":"第三条", ...}, ...],
          "synonym_replacements":    [("虚假出资","出资不实"), ...],
          "synonym_replaced":        替换后的文本（可能=normalized）,
          "queries":                 [primary, variant1, variant2],
          "where_hints":             {article_numbers:[], law_keywords:[], raw_articles:[...]},
        }
    """
    raw = query or ""
    norm = normalize_text(raw)
    intent, intent_hits = classify_intent(norm)
    laws = extract_law_names(norm)
    arts = extract_articles_structured(norm)
    syn_replaced, syn_hits = expand_synonyms(norm)
    queries = generate_queries(norm)
    wh = build_where_hints(norm)
    return {
        "raw": raw,
        "normalized": norm,
        "intent": intent,
        "intent_hits": intent_hits,
        "law_names": laws,
        "articles": arts,
        "synonym_replacements": syn_hits,
        "synonym_replaced": syn_replaced,
        "queries": queries,
        "where_hints": wh,
    }


# ==========================================================================
# 兼容 shim：保持 retrieval_service 现有 extract_article_number / detect_law_keywords 签名不变
# QU 模块引入后，retrieval_service 会改为走 understand().where_hints；
# 此处提供同名 shim 以便其他已 import 的代码不发生断裂。
# ==========================================================================

def extract_article_number(q: str) -> Optional[str]:
    arts = extract_articles_structured(q)
    return arts[0]["ar_form"] if arts else None


def detect_law_keywords(q: str) -> List[str]:
    return list(extract_law_names(q))
