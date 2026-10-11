# ============================================================
# LAW AG V6
# Answer Guard V3
# Evidence Constraint Checker
#
# 目标：
# 1. 检查答案是否超出 Evidence
# 2. 检查法条引用是否有 Evidence 支持
# 3. 检查来源引用是否有 Evidence 支持
# 4. 检查责任主体是否被无依据扩展
# 5. 检查明显的法律概念替换
# 6. 检查明显的条件关系改变
# 7. 计算 Evidence 覆盖率（0~1）
#
# 注意：
# Answer Guard 是“程序化风险提示器”，
# 不是法律事实判断器，也不是 LLM。
# ============================================================

import re
from typing import Dict, List, Tuple


# ============================================================
# 1. 法律概念替换
# ============================================================

SEMANTIC_REPLACEMENT = {
    "负有责任": [
        "存在过错",
        "有过错",
        "主观过错",
    ],
}


def check_semantic_replacement(answer):
    warnings = []

    answer = str(answer or "")

    for source, replacements in SEMANTIC_REPLACEMENT.items():
        if source in answer:
            for replacement in replacements:
                if replacement in answer:
                    warnings.append(
                        f"概念替换:{source}→{replacement}"
                    )

    return warnings


# ============================================================
# 2. 法条引用
# ============================================================

# 支持：
# 第180条
# 第 180 条
# 第五百六十二条
# 第五十三条
#
# 中文数字和阿拉伯数字均允许。
LAW_REFERENCE_PATTERN = r"第\s*[0-9零一二三四五六七八九十百千万亿]+\s*条"


# ============================================================
# 3. 来源关键词
#
# 注意：
# 这些词只用于检测“实际来源引用”，
# 不应该因为答案存在固定栏目：
#
# 【最高人民法院观点】
#
# 就触发报警。
# ============================================================

SOURCE_KEYWORDS = [
    "最高人民法院",
    "最高院",
    "指导案例",
]


# ============================================================
# 4. 责任主体
# ============================================================

SUBJECT_KEYWORDS = [
    "股东",
    "实际控制人",
    "董事",
    "监事",
    "高级管理人员",
    "受让人",
]


# ============================================================
# 5. 明显推论表达
# ============================================================

FORBIDDEN_INFERENCE = [
    "原则上",
    "通常",
    "一般认为",
    "可以认为",
    "应当理解为",
    "推定",
    "必然",
    "通常情况下",
    # 「当然」已移除：法律文本中多为正常连词（如「当然债务人」），
    # 2026-10-11 基准测试证实其误报（B3 夫妻共同债务题）。
]


# ============================================================
# 6. Evidence 外扩
#
# 这些词本身并不当然违法，
# 这里只作为“Evidence 可能没有支持”的风险提示。
# ============================================================

FORBIDDEN_EXTENSION = [
    "过错",
    "无过错",
    "举证责任",
    "证明责任",
    "监督义务",
    "注意义务",
    "善意",
    "恶意",
    "明知",
    "应知",
]


# ============================================================
# 7. 来源扩展
#
# 这些表达代表答案正在主动增加
# “最高人民法院”的权威来源。
# ============================================================

FORBIDDEN_SOURCE_EXPANSION = [
    "最高人民法院关于",
    "最高人民法院发布",
    "最高人民法院司法解释",
    "最高人民法院认为",
    "最高人民法院裁判",
]


# ============================================================
# 8. 法条弱引用 / 强引用
# ============================================================

ARTICLE_WEAK_WORDS = [
    "提及",
    "引用",
    "涉及",
    "参考",
]


ARTICLE_STRONG_WORDS = [
    "规定",
    "明确规定",
    "确立",
    "要求",
    "必须",
]


# ============================================================
# 9. 责任关系映射
#
# 只用于识别：
# Evidence 没有明确出现相关主体，
# 但答案却作出了特定责任判断。
# ============================================================

RESPONSIBILITY_MAP = {
    "股东": [
        "返还抽逃的出资",
    ],

    "董事": [
        "连带赔偿责任",
    ],

    "监事": [
        "连带赔偿责任",
    ],

    "高级管理人员": [
        "连带赔偿责任",
    ],
}


# ============================================================
# 10. 条件关系错误
# ============================================================

CONDITIONAL_ERRORS = [
    "造成损失的，应当返还",
    "造成损失后返还",
    "只有造成损失才返还",
]


# ============================================================
# 11. Evidence 标准化
# ============================================================

def normalize_evidence_text(evidence_text):
    """
    将 Evidence 统一转换成字符串。

    支持：
    1. str
    2. list[dict]
    3. list[str]
    4. dict
    """

    if evidence_text is None:
        return ""

    if isinstance(evidence_text, list):

        parts = []

        for item in evidence_text:

            if isinstance(item, dict):

                content = item.get("content", "")

                if content:
                    parts.append(str(content))

                # 某些 Evidence 结构可能把标题、
                # 来源等信息放在 metadata 中。
                for key in [
                    "law_name",
                    "citation_name",
                    "article",
                    "citation_article",
                    "source",
                    "source_document",
                    "authority",
                ]:

                    value = item.get(key)

                    if value:
                        parts.append(str(value))

            else:
                parts.append(str(item))

        return "\n".join(parts)

    if isinstance(evidence_text, dict):

        parts = []

        for key, value in evidence_text.items():

            if value:
                parts.append(str(value))

        return "\n".join(parts)

    return str(evidence_text)


# ============================================================
# 12. 判断答案中的法条引用
# ============================================================

def extract_article_references(answer):
    """
    提取答案中的所有法条引用。
    """

    answer = str(answer or "")

    return re.findall(
        LAW_REFERENCE_PATTERN,
        answer
    )


# ============================================================
# 13. 法条引用检查
#
# 注意：答案与证据中法条号可能混用中文数字/阿拉伯数字
# （如「第四十九条」vs「第49条」），匹配前必须归一化。
# ============================================================

_CN_DIGIT = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_num_to_int(text):

    """中文数字转整数，支持到「万亿」级；解析失败返回 None。"""

    if not text:

        return None

    # 纯阿拉伯数字直接返回
    if re.fullmatch(r"[0-9]+", text):

        return int(text)

    total = 0
    section = 0          # 当前「万/亿」段内的累计值
    number = 0           # 段内正在累积的系数

    for ch in text:

        if ch in _CN_DIGIT:

            number = _CN_DIGIT[ch]

        elif ch == "十":

            # 「十」无前置系数时视为 1（如「第十条」「第十五条」）
            section += (number or 1) * 10
            number = 0

        elif ch == "百":

            section += (number or 1) * 100
            number = 0

        elif ch == "千":

            section += (number or 1) * 1000
            number = 0

        elif ch == "万":

            section = (section + number) * 10000
            total += section
            section, number = 0, 0

        elif ch == "亿":

            section = (section + number) * 100000000
            total += section
            section, number = 0, 0

        else:

            return None

    result = total + section + number

    return result if result > 0 else None


def normalize_article_form(article):

    """将「第X条」统一为阿拉伯数字形式（第四十九条 → 第49条）。"""

    m = re.fullmatch(
        r"第\s*([0-9零一二三四五六七八九十百千万亿]+)\s*条",
        str(article or "").strip(),
    )

    if not m:

        return str(article or "").strip()

    n = _cn_num_to_int(m.group(1))

    return f"第{n}条" if n is not None else m.group(0)


def normalize_article_forms_in_text(text):

    """把文本中所有「第X条」统一为阿拉伯数字形式（供证据文本归一化）。"""

    return re.sub(
        r"第\s*([0-9零一二三四五六七八九十百千万亿]+)\s*条",
        lambda m: f"第{_cn_num_to_int(m.group(1))}条"
        if _cn_num_to_int(m.group(1)) is not None
        else m.group(0),
        str(text or ""),
    )


def check_article_reference(answer, evidence_text):

    warnings = []

    answer = str(answer or "")
    evidence_text = normalize_evidence_text(evidence_text)

    normalized_evidence = (
        normalize_article_forms_in_text(evidence_text)
        .replace(" ", "")
        .replace("\n", "")
        .replace("\t", "")
    )

    articles = extract_article_references(answer)

    for article in articles:

        normalized_article = normalize_article_form(article)

        if normalized_article not in normalized_evidence:

            warnings.append(
                f"法条无据:{article}"
            )

    return warnings


# ============================================================
# 14. 法条引用强度检查
#
# 只对特定高风险法条做检查。
# ============================================================

def check_article_strength(answer):

    warnings = []

    answer = str(answer or "")

    for article in [
        "第180条",
        "第192条",
    ]:

        if article not in answer:
            continue

        start = answer.find(article)

        # 查看法条前后的有限上下文
        context = answer[
            max(0, start - 20):
            min(len(answer), start + len(article) + 40)
        ]

        # 如果使用明显的弱引用词，
        # 不做严重报警，只提示。
        weak_found = any(
            word in context
            for word in ARTICLE_WEAK_WORDS
        )

        strong_found = any(
            word in context
            for word in ARTICLE_STRONG_WORDS
        )

        if weak_found and not strong_found:

            warnings.append(
                f"法条弱引用:{article}"
            )

    return warnings


# ============================================================
# 15. 来源检查
#
# 关键修复：
#
# 不再简单地看到“最高人民法院”就报警。
#
# 以下固定栏目不视为来源引用：
#
# 【最高人民法院观点】
#
# “本次检索证据未提供最高人民法院观点”
#
# 真正的来源表达例如：
#
# 最高人民法院认为……
# 最高人民法院指出……
# 根据最高人民法院……
# ============================================================

def check_source_reference(answer, evidence_text):

    warnings = []

    answer = str(answer or "")
    evidence_text = normalize_evidence_text(evidence_text)

    normalized_evidence = evidence_text.replace(
        " ",
        ""
    )

    source_patterns = [

        (
            "最高人民法院",
            [
                "最高人民法院认为",
                "最高人民法院指出",
                "最高人民法院明确",
                "最高人民法院认定",
                "根据最高人民法院",
                "最高人民法院判决",
                "最高人民法院裁判",
                "最高人民法院发布",
                "最高人民法院关于",
            ],
        ),

        (
            "最高院",
            [
                "最高院认为",
                "最高院指出",
                "根据最高院",
                "最高院判决",
                "最高院裁判",
            ],
        ),

        (
            "指导案例",
            [
                "指导案例",
            ],
        ),
    ]

    for source_name, patterns in source_patterns:

        for pattern in patterns:

            if pattern not in answer:
                continue

            if pattern not in normalized_evidence:

                warnings.append(
                    f"来源无据:{source_name}"
                )

                break

    return warnings


# ============================================================
# 16. 主体扩展检查
# ============================================================

def check_subject_extension(answer, evidence_text):

    warnings = []

    answer = str(answer or "")
    evidence_text = normalize_evidence_text(evidence_text)

    for subject in SUBJECT_KEYWORDS:

        if subject in answer:

            if subject not in evidence_text:

                warnings.append(
                    f"新增主体:{subject}"
                )

    return warnings


# ============================================================
# 17. 责任绑定检查
#
# 示例：
#
# Evidence：
# 董事……
#
# Answer：
# 董事承担连带赔偿责任
#
# 如果 Evidence 没有对应责任内容，
# 则提示。
# ============================================================

def check_responsibility_binding(answer, evidence_text):

    warnings = []

    answer = str(answer or "")
    evidence_text = normalize_evidence_text(evidence_text)

    for subject, responsibility_words in RESPONSIBILITY_MAP.items():

        if subject not in answer:
            continue

        for responsibility in responsibility_words:

            if responsibility in answer:

                if responsibility not in evidence_text:

                    warnings.append(
                        f"责任无据:{subject}→{responsibility}"
                    )

    return warnings


# ============================================================
# 18. 条件关系检查
# ============================================================

def check_conditional_relation(answer):

    warnings = []

    answer = str(answer or "")

    for phrase in CONDITIONAL_ERRORS:

        if phrase in answer:

            warnings.append(
                f"条件篡改:{phrase}"
            )

    return warnings


# ============================================================
# 19. Evidence 外扩检查
# ============================================================

def check_evidence_extension(answer, evidence_text):

    warnings = []

    answer = str(answer or "")
    evidence_text = normalize_evidence_text(evidence_text)

    for word in FORBIDDEN_EXTENSION:

        if word in answer:

            if word not in evidence_text:

                warnings.append(
                    f"观点无据:{word}"
                )

    return warnings


# ============================================================
# 20. 来源扩展检查
# ============================================================

def check_source_expansion(answer, evidence_text):

    warnings = []

    answer = str(answer or "")
    evidence_text = normalize_evidence_text(evidence_text)

    for phrase in FORBIDDEN_SOURCE_EXPANSION:

        if phrase in answer:

            if phrase not in evidence_text:

                warnings.append(
                    f"来源外扩:{phrase}"
                )

    return warnings


# ============================================================
# 21. Evidence 覆盖率
#
# 重要：
#
# 返回值必须是 0~1，
# 因为 server.py 使用：
#
# f"{guard_coverage:.0%}"
#
# 如果返回 50，
# 就会显示 5000%。
# ============================================================

def calculate_coverage(answer, evidence_text):
    """P1-2(b) 重写：用「条号 + 高频法律 4 字术语 + 重复 ≥ 2 次的中文 3~4 字核心词」计算证据覆盖率。

    旧实现问题：硬编码 12 个「抽逃出资/返还抽逃出资/董事/监事/第五十三条/…」
    关键词集合，结果导致非出资类题目 coverage 恒为 0%，Guard 永远在报 coverage=0% warning。

    新实现（通用、无行业依赖）：
      (a) 法条号 token（第X条/款/项）：在证据中出现即算命中（法律场景最高权威）
      (b) 法律 4 字词典（覆盖责任/救济/主体/程序/担保常见术语）× 答案实际存在 ∩ 证据实际存在
      (c) 答案中文 3~4 字 token 重复 ≥ 2 次的「强主题词」，同时在证据里也出现才算命中

    coverage 取值 [0, 1]。任何项为空则退化为 0.0；但拒答另有 honest_refusal 豁免通道。
    """
    _re = re

    answer = str(answer or "")
    evidence_text = normalize_evidence_text(evidence_text)

    if not answer or not evidence_text:
        return 0.0

    NUM = r"[0-9０-９零一二三四五六七八九十百千万两]"
    ART_RE = _re.compile(rf"第{NUM}{{1,10}}(?:条|款|项)")
    CN_RE = _re.compile(r"[\u4e00-\u9fff]")

    # ------------------------------------------------------------------
    # (a) 条号
    # ------------------------------------------------------------------
    ans_art = {m.group(0) for m in ART_RE.finditer(answer)}
    ev_art = {m.group(0) for m in ART_RE.finditer(evidence_text)}
    core_pairs: List[Tuple[str, str]] = []  # [(category, token)]
    for t in ans_art:
        core_pairs.append(("art", t))

    # ------------------------------------------------------------------
    # (b) 4 字法律术语（答案×证据交叉命中才算有效核心词）
    # ------------------------------------------------------------------
    LEGAL_4GRAM = (
        # 责任类
        "连带责任", "补充赔偿", "赔偿责任", "违约责任", "缔约过失", "侵权责任",
        "过错责任", "严格责任", "举证责任", "证明责任",
        # 救济类
        "解除合同", "合同解除", "撤销合同", "继续履行", "采取补救", "违约金",
        "损害赔偿", "定金罚则", "不可抗力", "情势变更",
        "代位求偿", "代位行使", "债权人撤销", "债权人代位",
        # 主体类
        "有限责任", "股份有限", "一人公司", "法定代表", "高级管理",
        "忠实义务", "勤勉义务", "出资不实", "虚假出资", "抽逃出资",
        "出资加速", "出资义务", "股权回购", "股权转让", "司法解散",
        "公司解散", "公司清算", "破产清算", "破产重整", "破产和解",
        # 财产类
        "差额填补", "补充责任", "相应责任", "出资范围", "本息范围",
        "补充赔偿", "发起人连带", "董事责任", "监事责任", "高管责任",
        # 担保类
        "保证责任", "抵押担保", "质押担保", "留置担保", "担保物权",
        "所有权保留",
        # 诉讼/仲裁
        "诉讼时效", "除斥期间", "管辖法院", "管辖权异议", "财产保全",
        "证据保全", "先予执行", "执行程序", "仲裁协议",
        # 合同类
        "合同效力", "合同无效", "可撤销合同", "效力待定", "无权代理",
        "表见代理", "善意取得", "不当得利", "无因管理",
    )
    for t in LEGAL_4GRAM:
        if t in answer:
            core_pairs.append(("legal4", t))

    # ------------------------------------------------------------------
    # (c) 答案高频 3~4 字中文 token（重复 ≥ 2 次、非停用词）
    # ------------------------------------------------------------------
    STOP_TOKENS = {
        "那么", "但是", "然而", "或者", "以及", "如果", "因此", "所以",
        "可以", "可能", "应当", "需要", "没有", "一个", "这样", "那样",
        "这些", "那些", "这个", "那个", "进行", "通过", "根据", "对于",
        "关于", "作为", "成为", "表示", "提供", "使用", "相关", "一定",
        "已经", "不是", "不能", "不会", "一种", "两种", "以上", "以下",
        "之内", "之前", "之后", "之间", "法律", "规定", "情况", "行为",
        "责任", "公司", "股东", "合同", "法院",
    }
    ans_cn = "".join(CN_RE.findall(answer))
    counts: Dict[str, int] = {}
    for w in (3, 4):
        for i in range(0, max(0, len(ans_cn) - w + 1)):
            tok = ans_cn[i:i + w]
            if tok in STOP_TOKENS:
                continue
            counts[tok] = counts.get(tok, 0) + 1
    for tok, cnt in counts.items():
        if cnt >= 2:
            core_pairs.append(("freq", tok))

    # ------------------------------------------------------------------
    # 构建证据查找集合（条号 + 全部 2-6 字中文 n-gram）
    # ------------------------------------------------------------------
    EVID_TOKENS: set[str] = set(ev_art)
    ev_cn = "".join(CN_RE.findall(evidence_text))
    for w in (2, 3, 4, 5, 6):
        for i in range(0, max(0, len(ev_cn) - w + 1)):
            EVID_TOKENS.add(ev_cn[i:i + w])

    if not core_pairs:
        return 0.0
    hit = 0
    for _cat, tok in core_pairs:
        if tok in EVID_TOKENS:
            hit += 1
    ratio = hit / len(core_pairs)
    return round(ratio, 4)


# ==========================================================================
# 诚实拒答识别（P1-2(b) 豁免）
# ==========================================================================

_HONEST_REFUSAL_PATTERNS = (
    "超出了我的知识范围",
    "不在我的知识范围内",
    "超出了我的知识库",
    "超出了知识范围",
    "不在知识范围内",
    "当前知识",
    "未能在当前知识库",
    "未能检索到相关",
    "未检索到相关法律",
    "未检索到相关证据",
    "未找到相关法律",
    "未找到相关规定",
    "无法提供具体",
    "我无法提供",
    "我无法判断",
    "我无法回答",
    "建议咨询专业律师",
    "本法律知识库未收录",
    "本知识库未收录",
    "目前没有相关资料",
    "目前未收录",
)


def is_honest_refusal(answer) -> bool:
    """当 LLM 在 RAG 证据不够时主动「承认我不知道」，Guard 应视为合法输出，
    不触发 passed=False / coverage=0% 的处罚。"""
    text = str(answer or "")
    return any(p in text for p in _HONEST_REFUSAL_PATTERNS)


# ============================================================
# 22. 推论检查
# ============================================================

def check_inference(answer):

    warnings = []

    answer = str(answer or "")

    for word in FORBIDDEN_INFERENCE:

        if word in answer:

            warnings.append(
                f"推论表达:{word}"
            )

    return warnings


# ============================================================
# 23. Answer Guard 主入口
# ============================================================

def check_answer(
    answer,
    evidence_text=None,
):

    warnings = []

    answer = str(answer or "")
    evidence_text = normalize_evidence_text(
        evidence_text
    )

    # --------------------------------------------------------
    # 1. 法律概念替换
    # --------------------------------------------------------

    warnings.extend(
        check_semantic_replacement(
            answer
        )
    )

    # --------------------------------------------------------
    # 2. 推论表达
    # --------------------------------------------------------

    warnings.extend(
        check_inference(
            answer
        )
    )

    # --------------------------------------------------------
    # 3. 条件关系
    # --------------------------------------------------------

    warnings.extend(
        check_conditional_relation(
            answer
        )
    )

    # --------------------------------------------------------
    # 4. 法律引用强度
    # --------------------------------------------------------

    warnings.extend(
        check_article_strength(
            answer
        )
    )

    # --------------------------------------------------------
    # 5. 责任绑定
    # --------------------------------------------------------

    warnings.extend(
        check_responsibility_binding(
            answer,
            evidence_text
        )
    )

    # --------------------------------------------------------
    # 6. 主体扩展
    # --------------------------------------------------------

    warnings.extend(
        check_subject_extension(
            answer,
            evidence_text
        )
    )

    # --------------------------------------------------------
    # 7. Evidence 外扩
    # --------------------------------------------------------

    warnings.extend(
        check_evidence_extension(
            answer,
            evidence_text
        )
    )

    # --------------------------------------------------------
    # 8. 来源扩展
    # --------------------------------------------------------

    warnings.extend(
        check_source_expansion(
            answer,
            evidence_text
        )
    )

    # --------------------------------------------------------
    # 9. Evidence 覆盖率
    # --------------------------------------------------------

    coverage = calculate_coverage(
        answer,
        evidence_text
    )

    # --------------------------------------------------------
    # 10. 法条引用
    # --------------------------------------------------------

    warnings.extend(
        check_article_reference(
            answer,
            evidence_text
        )
    )

    # --------------------------------------------------------
    # 11. 来源检查
    # --------------------------------------------------------

    warnings.extend(
        check_source_reference(
            answer,
            evidence_text
        )
    )

    # --------------------------------------------------------
    # 去重
    # --------------------------------------------------------

    unique_warnings = []

    for warning in warnings:

        if warning not in unique_warnings:

            unique_warnings.append(
                warning
            )

    warnings = unique_warnings

    # --------------------------------------------------------
    # P1-2(b) 诚实拒答豁免：LLM 在证据不足时明确回答「不知道 / 请咨询律师」
    #   → 视为合法输出，passed=True，coverage 单独记录为 None 显式标记
    # --------------------------------------------------------

    honest = is_honest_refusal(answer)
    if honest:
        passed_flag = True
        coverage_report: Optional[float] = None  # 显式不适用
        warning_report = [
            f"（诚实拒答）{w}" for w in warnings
        ] or ["（诚实拒答）LLM 明确声明不了解该问题，属于安全输出。"]
    else:
        # ----------------------------------------------------
        # 严重度分级（2026-10-11 Guard 调优）：
        #   硬警告 → 幻觉/无据引用类，一票否决 passed
        #   「观点无据」→ 术语级检查（举证责任/善意/过错等），
        #     法律论述的正常术语使用常被字面匹配误伤
        #     （如证据用「谁主张谁举证」表述，答案写「举证责任」），
        #     降级为提示性警告：保留展示，不参与 passed 判定。
        # ----------------------------------------------------
        hard_warnings = [
            w for w in warnings if not w.startswith("观点无据:")
        ]
        passed_flag = len(hard_warnings) == 0
        coverage_report = coverage if isinstance(coverage, (int, float)) else None
        warning_report = list(warnings)

    return {

        "passed":
            passed_flag,

        "warning_count":
            len(warning_report),

        "warnings":
            warning_report,

        "evidence_coverage":
            coverage_report,

        "honest_refusal":
            honest,
    }
