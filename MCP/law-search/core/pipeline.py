"""
core/pipeline.py — LegalPipeline 端到端法律问答管线

将以下 6 个阶段封装为单一入口：

    phase 1: 检索（core.retrieval_service — Chroma + Embedding + Reranker）
        ↓ evidence_items
    phase 2: 证据融合（core.evidence_fusion.build_evidence_pack）
        ↓ evidence_pack
    phase 3: 引用构建（core.citation_builder + core.citation_formatter）
        ↓ lawyer_citations
    phase 4: 上下文构建（core.evidence_context.build_evidence_context）
        ↓ evidence_context
    phase 5: Prompt 构建（core.answer_prompt.build_answer_prompt）
        ↓ prompt
    phase 6: LLM 生成 + Answer Guard 校验
        ↓ 结构化结果 + 最终字符串答案

对外 API：
    pipeline = LegalPipeline()      # 懒加载所有模型
    result = pipeline.ask(question, top_k=8)
    # result = {
    #   "question":         str,
    #   "evidence_items":   list,       # (phase 1)
    #   "evidence_pack":    list,       # (phase 2)
    #   "citations":        list,       # (phase 3)
    #   "context":          str,        # (phase 4)
    #   "prompt":           str,        # (phase 5)
    #   "answer":           str,        # (phase 6, LLM 原始答案)
    #   "final":            str,        # (phase 6, 含 Guard Warning Block 的最终答案)
    #   "guard_passed":     bool,
    #   "guard_warnings":   list[str],
    #   "guard_coverage":   float | None,
    #   "timing_s":         dict[str, float],
    #   "total_s":          float,
    # }

    # 便捷函数（与 server.py 的 search_law 输出格式相同）
    answer_str = pipeline.search_law(question, top_k=8)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import yaml
from typing import Any, Callable, Dict, List, Optional

from core.answer_guard import check_answer
from core.answer_prompt import build_answer_prompt
from config import INTERNAL_GENERATION_PROVIDER
from core.citation_builder import build_citations
from core.citation_formatter import format_lawyer_citations
from core.common import wrap_law_name
from core.evidence_context import build_evidence_context
from core.evidence_fusion import build_evidence_pack
from core.llm_client import DEFAULT_ROUTER, chat_completion, chat_completion_stream
from core.retrieval_service import LegalRetriever, get_retriever
from core.web_search import web_search as _web_search

logger = logging.getLogger("law-search.pipeline")

# ------------------------------------------------------------------
# L3 时效复核配置（watchlist.yaml；缺失/损坏时静默禁用）
# ------------------------------------------------------------------

_WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"
_WATCHLIST_CACHE: Optional[Dict[str, Any]] = None


def _load_watchlist() -> Dict[str, Any]:
    """加载 watchlist.yaml（模块级缓存；失败 → disabled）。"""
    global _WATCHLIST_CACHE
    if _WATCHLIST_CACHE is not None:
        return _WATCHLIST_CACHE
    default = {"enabled": False, "trigger_keywords": [], "laws": [], "max_results": 4}
    try:
        with open(_WATCHLIST_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        _WATCHLIST_CACHE = {**default, **cfg}
        # YAML 会把 2023/2024 等年份关键字解析为 int，统一转 str
        _WATCHLIST_CACHE["trigger_keywords"] = [
            str(k) for k in (_WATCHLIST_CACHE.get("trigger_keywords") or [])
        ]
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("watchlist 加载失败，L3 时效复核禁用：%s", exc)
        _WATCHLIST_CACHE = default
    return _WATCHLIST_CACHE

# 流式增量回调：on_delta(piece: str) -> None（预览用途；最终答案以返回值为准）
DeltaCallback = Optional[Callable[[str], None]]


class LegalPipeline:
    """法律问答端到端管线。模型懒加载，安全反复实例化。"""

    def __init__(
        self,
        retriever: Optional[LegalRetriever] = None,
        max_evidence_context: int = 5,
        max_result_chars: int = 5200,
    ) -> None:
        self._retriever = retriever or get_retriever()
        self._max_evidence_context = max_evidence_context
        self._max_result_chars = max_result_chars

    # ------------------------------------------------------------------
    # AGENT STOP HEADER + 法条原文清单（放返回值最开头，强抑制反复调用）
    # ------------------------------------------------------------------

    @staticmethod
    def _build_agent_stop_header(
        evidence_pack: List[Dict[str, Any]],
        max_items: int = 10,
        max_chars_per_item: int = 900,
    ) -> str:
        """从 evidence_pack 提取去重的法条原文，放在返回值第 1 行。

        核心架构修复（2026-09-01 实测结论）：
            之前把原文附录放在 final 末尾，LLM/Agent 在 ReAct loop 中读完
            开头的【分析答案摘要】后就立刻进入"我需要查证原文"状态，
            根本不会翻到末尾去看附录 → 继续 14+ 次调用 get_provision。

            解决：把【AGENT LOOP STOP 信号】 + 【完整法条原文清单】
            放在返回值的 最前 1-N 行，Agent 第 1 眼就看到：
                (a) 明确的停止再调信号；
                (b) 结构化的法名+条文号+原文 表格；
            → 直接结束工具调用链，进入最终答案撰写阶段。

        去重键：(law_name_norm, article_norm)。
        """
        if not evidence_pack:
            return (
                "================================================================\n"
                " 【AGENT LOOP · STOP SIGNAL — search_law 无结果】\n"
                " — 相同/近似关键词严禁第二次调用 search_law（结果一致）。\n"
                " — 严禁立刻自动调用任何 chinese-law / search_external 工具。\n"
                " — 正确流程：先向用户说明「本地知识库没有命中」，并主动询问用户：\n"
                "    (a) 是否可以提供更具体的关键词（如具体法律名称 / 条文编号 / 领域）；\n"
                "    (b) 是否允许你调用第二层兜底（chinese-law 系列，限 1 次）；\n"
                "    (c) 是否允许调用末位兜底（search_external / Tavily，消耗配额，限 1 次）。\n"
                " — 只有在用户明确同意 (b) 或 (c) 后，才允许调用对应工具 1 次。\n"
                "================================================================\n\n"
                "【检索结果】本次 search_law 在本地向量库中未命中任何相关法条。\n\n"
            )

        def _norm(s: Any) -> str:
            return str(s or "").strip().replace("\u3000", " ").replace(" ", "")

        def _cell_escape(s: str) -> str:
            # Markdown 表格单元格里不允许裸换行和 |
            return s.replace("|", "｜").replace("\r\n", "\n").replace("\n", " ⏎ ")

        seen: set[tuple[str, str]] = set()
        rows: list[str] = []
        total_chars = 0
        header_cap = 3600  # header 整体上限（char）

        for idx, ev in enumerate(evidence_pack, start=1):
            if len(rows) >= max_items:
                break
            if not isinstance(ev, dict):
                continue

            law_name = str(
                ev.get("law_name")
                or ev.get("source_document")
                or ev.get("title")
                or ""
            ).strip()
            article = str(
                ev.get("article")
                or ev.get("citation_article")
                or ev.get("article_list")
                or ""
            ).strip()
            content = str(ev.get("content") or "").strip()

            if not content:
                continue
            if not law_name and not article:
                # 至少要有法名或条文号，否则没法作为可引用的法条原文条目
                law_name = law_name or f"关联资料{idx}"

            key = (_norm(law_name), _norm(article))
            if key in seen:
                continue
            seen.add(key)

            # 规范化条文号（便于 Agent 识别"第X条"格式）
            if (
                article
                and article not in ("—", "-", "–")
                and not (article.startswith("第") and "条" in article[:12])
            ):
                article = f"第{article}条"

            # 单条内容截长
            if len(content) > max_chars_per_item:
                content = content[:max_chars_per_item] + "…（本条片段）"

            law_cell = _cell_escape(wrap_law_name(law_name)) or "—"
            art_cell = _cell_escape(article) or "—"
            cont_cell = _cell_escape(content)

            row = f"| {law_cell} | {art_cell} | {cont_cell} |"
            # 预估 + 分隔线（每条 3 字符左右的换行开销）
            if total_chars + len(row) + 3 > header_cap:
                break
            rows.append(row)
            total_chars += len(row) + 3

        if not rows:
            # 虽然有 evidence_pack，但因为去重/过滤后没能提取出可用法条条目
            return (
                "【search_law 执行完毕】命中资料但未能提取结构化法条。\n"
                "禁止再用相同参数重复调用 search_law；请使用以下『分析答案』部分的内容作答，\n"
                "或仅在确实缺失关键信息时才调用下一层兜底工具。\n\n"
            )

        table_header = "| 法条名 | 条款号 | 原文内容 |"
        table_sep    = "|---|---|---|"

        header = (
            "================================================================\n"
            f" 【AGENT LOOP · MANDATORY STOP SIGNAL — 共有 {len(rows)} 条命中法条原文】\n"
            "\n"
            " 🔴 严格禁止（违反会触发循环调用 + 超时 ）：\n"
            "    1) search_law（本工具）：相同 / 近似关键词严禁第二次调用。\n"
            "    2) chinese-law 系列全部 8 个工具：search_legislation / get_provision /\n"
            "       list_sources / validate_citation / build_legal_stance / format_citation /\n"
            "       check_currency / about —— 全部禁止直接调用。\n"
            "       【chinese-law 调用前置门槛（必须 3 条同时满足 · AND）】：\n"
            "         a) 本 STOP SIGNAL 明确写了『未命中任何相关法条』或下方表格内容完全不相关\n"
            "            （注意：『内容不够详细 / 不够多』≠ 不相关，不属于允许调用的理由）；\n"
            "         b) 用户在当前轮对话明确提出：search_law 结果不够，请使用 chinese-law 补充，\n"
            "            或明确指出需要一部表格中完全没有的偏门法律；\n"
            "         c) 同一用户问题下，chinese-law 系列工具此前从未被调用过（限 1 次 / 问题）。\n"
            "       以上 3 条缺任何一条，都不得调用 chinese-law 的任何工具。\n"
            "    3) search_case：除非用户在问题中明确提出『找案例 / 判决书 / 类似案件 /\n"
            "       指导案例』等字样，否则严禁调用。\n"
            "    4) search_external（Tavily）：严禁用于查已知法律的具体条文；只有 \n"
            "       search_law + chinese-law（如获同意）均明确无结果，或用户明确问最新 \n"
            "       动态 / 新闻时，才允许调用 1 次。\n"
            "\n"
            " 🔴 累计工具调用次数（含本条 search_law）不得超过 3 次。\n"
            "    到 3 次必须立刻停止一切工具调用，直接依据下表法条原文生成最终答案。\n"
            "================================================================\n\n"
            f"{table_header}\n"
            f"{table_sep}\n"
            + "\n".join(rows)
            + "\n\n================================================================\n"
            " 以上为本次 search_law 提取的全部命中法条原文表格。\n"
            " 请直接依据上表内容撰写你的最终答案；正文引用法条时统一使用\n"
            " 《法名》第N条 格式（如《公司法》第232条），条款号以上表为准。\n"
            "================================================================\n\n"
            "--- 分析答案 ---\n\n"
        )
        return header

    # ------------------------------------------------------------------
    # L3 时效复核（服务端内联 web_search；不增加 Agent 工具调用次数）
    # ------------------------------------------------------------------

    @staticmethod
    def _currency_recheck_query(question: str) -> Optional[str]:
        """时效敏感 + 命中观察清单 → 返回 Tavily 查询串；否则 None。

        触发条件（AND，控制 Tavily 免费额度消耗）：
            1) 问题含 watchlist.trigger_keywords 之一；
            2) 问题命中 watchlist.laws[].name / aliases 之一。
        """
        cfg = _load_watchlist()
        if not cfg.get("enabled"):
            return None
        q = (question or "").strip()
        keywords = cfg.get("trigger_keywords") or []
        if not any(str(k) in q for k in keywords):
            return None
        # 最长名优先（"民法典合同编通则司法解释" 优先于 "民法典"）
        best: Optional[tuple] = None
        for law in cfg.get("laws") or []:
            if not isinstance(law, dict):
                continue
            names = [str(law.get("name") or "")]
            names += [str(a) for a in (law.get("aliases") or [])]
            for n in names:
                if n and n in q and (best is None or len(n) > len(best[0])):
                    best = (n, law)
        if best:
            law = best[1]
            return (
                str(law.get("query") or "").strip()
                or f"{best[0]} 最新修订 现行有效 施行日期"
            )
        return None

    def _phase5b_currency_recheck(self, question: str) -> str:
        """时效复核块（追加进 Prompt 的 Evidence Context 末尾）。

        约束：
            - 单次 web_search（basic 深度，≤ max_results 条）；
            - 任何异常静默跳过，绝不阻断/拖垮 search_law 主流程；
            - 对 LLM/Agent 而言 search_law 仍是 1 次工具调用，
              不违反工具收敛铁律（STOP SIGNAL 语义不变）。
        """
        try:
            query = self._currency_recheck_query(question)
            if not query:
                return ""
            cfg = _load_watchlist()
            text, meta = _web_search(
                query,
                max_results=int(cfg.get("max_results", 4)),
                search_depth="basic",
            )
            if meta.get("error"):
                logger.warning("Currency recheck failed: %s", meta.get("error"))
                return ""
            logger.info(
                "Currency recheck | query=%r | chars=%s",
                query,
                len(text or ""),
            )
            return (
                "\n\n==================================================\n"
                "【时效复核 · 网络交叉验证（仅供参考）】\n"
                f"复核查询：{query}\n\n"
                f"{text}\n"
                "使用规则：以上内容仅用于核对相关法律的现行有效性、\n"
                "最新修订版本与施行日期；若与上述 Evidence 冲突，\n"
                "一律以 Evidence 为准，并可向用户说明本地知识库\n"
                "版本可能滞后、建议以官方发布为准。\n"
                "=================================================="
            )
        except Exception as exc:  # 复核绝不阻断主流程
            logger.warning("Currency recheck skipped: %s", exc)
            return ""

    def warmup(self) -> None:
        """预加载检索层全部懒加载组件（幂等，见 LegalRetriever.warmup）。

        常驻 MCP 服务（DSH）启动时后台调用，用户首次 search_law 免付
        embedding/reranker/Chroma 冷启动（实测 ~3 分钟）。"""
        self._retriever.warmup()

    # ------------------------------------------------------------------
    # 内部阶段
    # ------------------------------------------------------------------

    def _phase1_retrieve(
        self, question: str, top_k: int
    ) -> tuple[List[Dict[str, Any]], Dict[str, float]]:
        """P1: Embedding → Chroma → Rerank → Evidence Items."""
        return self._retriever.search_and_rerank(question, top_k=top_k)

    def _phase2_fuse(
        self, evidence_items: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """P2: Evidence Fusion（身份规范化 + 证据打分）。"""
        return build_evidence_pack(evidence_items)

    def _phase3_citations(
        self, evidence_pack: List[Dict[str, Any]]
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """P3: build_citations → format_lawyer_citations。"""
        raw = build_citations(evidence_pack)
        formatted = format_lawyer_citations(raw)
        return raw, formatted

    def _phase4_context(
        self, evidence_pack: List[Dict[str, Any]]
    ) -> str:
        """P4: Evidence Context。"""
        return build_evidence_context(
            evidence_pack, max_items=self._max_evidence_context
        )

    def _phase5_prompt(
        self,
        question: str,
        lawyer_citations: List[Dict[str, Any]],
        context: str,
    ) -> str:
        """P5: Build Answer Prompt。"""
        return build_answer_prompt(question, lawyer_citations, context)

    def _phase6_llm_and_guard(
        self,
        prompt: str,
        evidence_text_or_list: Any,
        on_delta: DeltaCallback = None,
    ) -> tuple[str, Dict[str, Any], bool, List[str], Optional[float]]:
        """P6: LLM → Answer Guard → (answer, guard_result, passed, warnings, coverage)。

        on_delta 提供时走流式补全（逐段回调增量），Guard 仍基于完整文本校验。
        """
        if on_delta is None:
            answer = DEFAULT_ROUTER.chat_completion(
                prompt,
                provider_hint=INTERNAL_GENERATION_PROVIDER,
            )
        else:
            answer = DEFAULT_ROUTER.chat_completion_stream(
                prompt,
                on_delta=on_delta,
                provider_hint=INTERNAL_GENERATION_PROVIDER,
            )

        guard = check_answer(answer, evidence_text=evidence_text_or_list)
        if isinstance(guard, dict):
            passed = bool(guard.get("passed", False))
            warnings = list(guard.get("warnings", []) or [])
            coverage = guard.get("evidence_coverage", None)
        elif isinstance(guard, list):
            passed = len(guard) == 0
            warnings = list(guard)
            coverage = None
        else:
            passed = False
            warnings = [f"Guard 返回异常类型: {type(guard).__name__}"]
            coverage = None

        return answer, guard if isinstance(guard, dict) else {}, passed, warnings, coverage

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def ask(
        self,
        question: str,
        *,
        top_k: int = 8,
        return_prompt: bool = True,
        on_delta: DeltaCallback = None,
    ) -> Dict[str, Any]:
        """
        完整端到端法律问答。

        Parameters:
            question:     用户法律问题字符串
            top_k:        返回的证据数量（默认 8）
            return_prompt: 是否在结果中包含最终 prompt（调试用）
            on_delta:     流式增量回调 on_delta(piece)（LLM 生成阶段逐段推送）；
                          None → 走非流式路径（行为与旧版完全一致）

        Returns:
            结构化结果 dict（见模块文档注释）。
        """
        t_overall = time.perf_counter()
        clean_q = (question or "").strip()
        if not clean_q:
            return {
                "question": "",
                "evidence_items": [],
                "evidence_pack": [],
                "citations": [],
                "context": "",
                "prompt": "",
                "answer": "",
                "final": "查询内容为空。请提供需要检索的法律问题或关键词。",
                "guard_passed": True,
                "guard_warnings": [],
                "guard_coverage": None,
                "timing_s": {},
                "total_s": 0.0,
            }

        logger.info("=" * 72)
        logger.info("Pipeline.ask | top_k=%s | question=%r", top_k, clean_q)

        timing: Dict[str, float] = {}

        # ----- Phase 1: 检索 -----
        t = time.perf_counter()
        evidence_items, ret_timing = self._phase1_retrieve(clean_q, top_k)
        timing["phase1_retrieve_s"] = time.perf_counter() - t
        timing.update({f"retrieve_{k}": v for k, v in ret_timing.items()})

        if not evidence_items:
            empty_header = (
                "【检索结果 · 空】search_law 在本地法律知识库中未命中任何相关法条。\n"
                "禁止再以相同/近似参数重复调用 search_law（会得到同样结果）。\n"
                "如果确实需要补充，可以：\n"
                "  · 尝试更宽泛的关键词重新问用户；\n"
                "  · 或仅在下一层兜底工具调用 1 次（不得反复）。\n\n"
                "---\n\n"
            )
            empty_final = empty_header + (
                "未找到相关法律资料。当前本地法律知识库没有匹配结果。"
            )
            return {
                "question": clean_q,
                "evidence_items": [],
                "evidence_pack": [],
                "citations": [],
                "context": "",
                "prompt": "",
                "answer": "",
                "final": empty_final,
                "guard_passed": True,
                "guard_warnings": [],
                "guard_coverage": 0.0,
                "timing_s": timing,
                "total_s": time.perf_counter() - t_overall,
            }

        # ----- Phase 2: Evidence Fusion -----
        t = time.perf_counter()
        evidence_pack = self._phase2_fuse(evidence_items)
        timing["phase2_fusion_s"] = time.perf_counter() - t
        logger.info("Fusion: %s items → %s pack entries", len(evidence_items), len(evidence_pack))

        # ----- Phase 3: Citations -----
        t = time.perf_counter()
        raw_citations, lawyer_citations = self._phase3_citations(evidence_pack)
        timing["phase3_citation_s"] = time.perf_counter() - t
        logger.info("Citations: %s raw → %s formatted", len(raw_citations), len(lawyer_citations))

        # ----- Phase 4: Context -----
        t = time.perf_counter()
        context = self._phase4_context(evidence_pack)
        timing["phase4_context_s"] = time.perf_counter() - t
        logger.info("Context: %s chars", len(context))

        # ----- Phase 4b: L3 时效复核（服务端内联，不增加工具调用次数）-----
        t = time.perf_counter()
        currency_block = self._phase5b_currency_recheck(clean_q)
        timing["phase4b_currency_s"] = time.perf_counter() - t
        if currency_block:
            logger.info(
                "Currency recheck block: %s chars appended to prompt",
                len(currency_block),
            )

        # ----- Phase 5: Prompt -----
        t = time.perf_counter()
        prompt = self._phase5_prompt(clean_q, lawyer_citations, context + currency_block)
        timing["phase5_prompt_s"] = time.perf_counter() - t
        logger.info("Prompt: %s chars", len(prompt))

        # ----- Phase 6: LLM + Guard -----
        t = time.perf_counter()
        answer, _guard_obj, guard_passed, guard_warnings, guard_coverage = (
            self._phase6_llm_and_guard(prompt, evidence_pack, on_delta=on_delta)
        )
        timing["phase6_llm_guard_s"] = time.perf_counter() - t
        logger.info(
            "LLM+Guard: passed=%s warnings=%s coverage=%s answer=%s chars",
            guard_passed, len(guard_warnings), guard_coverage, len(answer),
        )

        # ----- 最开头：AGENT STOP HEADER + 法条原文表格（关键架构修复）-----
        # 放在 final 第 1 行，保证 Agent 第 1 眼就看到停止信号 + 原文清单，
        # 不会读完分析摘要后再去 14+ 次 get_provision 补原文。
        stop_header = self._build_agent_stop_header(evidence_pack)

        # ----- Guard Warning Block（接在 LLM 分析答案之后）-----
        answer = (answer or "").strip()
        if guard_warnings:
            coverage_str = (
                f"（证据覆盖率 {guard_coverage:.0%}）"
                if isinstance(guard_coverage, (int, float)) else ""
            )
            warning_block = (
                "\n\n---\n"
                f"⚠️ 以下内容未在证据中完全核实{coverage_str}，仅供参考：\n"
                + "\n".join(f"  • {w}" for w in guard_warnings)
            )
            answer_body = answer + warning_block
        else:
            answer_body = answer

        # ----- 最终拼接顺序：[HEADER] → [分析答案] → [整体截断] -----
        final = stop_header + answer_body

        # ----- 截断过长 -----
        max_chars = self._max_result_chars
        if len(final) > max_chars:
            final = final[:max_chars] + "\n...(内容截断)"

        result: Dict[str, Any] = {
            "question": clean_q,
            "evidence_items": evidence_items,
            "evidence_pack": evidence_pack,
            "citations": lawyer_citations,
            "context": context,
            "answer": answer,
            "final": final,
            "guard_passed": guard_passed,
            "guard_warnings": guard_warnings,
            "guard_coverage": guard_coverage,
            "timing_s": timing,
            "total_s": time.perf_counter() - t_overall,
        }

        if not return_prompt:
            result.pop("prompt", None)  # backward compat
        else:
            result["prompt"] = prompt

        logger.info(
            "Pipeline done | evidence=%s | guard=%s/%s warn | chars=%s | total=%.2fs",
            len(evidence_pack),
            "✓" if guard_passed else "✗",
            len(guard_warnings),
            len(final),
            result["total_s"],
        )
        return result

    # ------------------------------------------------------------------
    # server.py 兼容：仅返回最终字符串
    # ------------------------------------------------------------------

    def search_law(
        self,
        question: str,
        top_k: int = 8,
        on_delta: DeltaCallback = None,
    ) -> str:
        """MCP server.py 兼容接口：只返回最终字符串答案。

        on_delta 提供时，LLM 生成阶段逐段推送流式增量（预览）；
        返回值仍为经 Guard 校验的完整答案。
        """
        if not (question or "").strip():
            return "查询内容为空。请提供需要检索的法律问题或关键词。"
        result = self.ask(question, top_k=top_k, return_prompt=False, on_delta=on_delta)
        return result["final"]

    # ------------------------------------------------------------------
    # write_litigation：法律文书起草（起诉状/答辩状/代理词/法律意见书）
    # ------------------------------------------------------------------

    def write_litigation(
        self,
        doc_type: str,
        case_facts: str,
        *,
        parties: str = "",
        claims: str = "",
        court: str = "",
        top_k: int = 8,
        on_delta: DeltaCallback = None,
    ) -> Dict[str, Any]:
        """文书起草管线：案情 → 法条检索 → 模板化 prompt → LLM 生成 → 引用校验。

        与 ask() 的差异：
            - 不跑 answer_guard（问答覆盖率语义不适用于文书），
              改用 litigation_builder.check_citations 引用来源一致性校验
            - 文书不截断（完整交付）
        """
        from core.litigation_builder import (
            build_litigation_prompt,
            build_retrieval_query,
            check_citations,
            normalize_doc_type,
        )

        t_overall = time.perf_counter()
        timing: Dict[str, float] = {}

        key = normalize_doc_type(doc_type)
        facts = (case_facts or "").strip()
        if key is None:
            return {
                "ok": False,
                "doc_type": doc_type,
                "document": "",
                "final": (
                    "未知文书类型。支持：complaint/起诉状、defense/答辩状、"
                    "representation/代理词、legal_opinion/法律意见书。"
                ),
                "citation_warnings": [],
                "citations": [],
                "timing_s": {},
                "total_s": time.perf_counter() - t_overall,
            }
        if not facts:
            return {
                "ok": False,
                "doc_type": key,
                "document": "",
                "final": "案情事实为空。请提供案件事实描述（case_facts）后重试。",
                "citation_warnings": [],
                "citations": [],
                "timing_s": {},
                "total_s": time.perf_counter() - t_overall,
            }

        from core.litigation_builder import DOC_TEMPLATES

        logger.info(
            "Pipeline.write_litigation | doc_type=%s | facts=%s chars | top_k=%s",
            key, len(facts), top_k,
        )

        # ----- 检索（复用 P1-P4，检索 query 由案情+诉求构建）-----
        query = build_retrieval_query(facts, claims)
        t = time.perf_counter()
        evidence_items, ret_timing = self._phase1_retrieve(query, top_k)
        timing["phase1_retrieve_s"] = time.perf_counter() - t
        timing.update({f"retrieve_{k}": v for k, v in ret_timing.items()})

        if not evidence_items:
            return {
                "ok": False,
                "doc_type": key,
                "document": "",
                "final": (
                    "未在本地法律知识库中检索到与案情相关的法条，无法起草。"
                    "请确认知识库已收录相关法律（可用 chroma_status 查看）。"
                ),
                "citation_warnings": [],
                "citations": [],
                "timing_s": timing,
                "total_s": time.perf_counter() - t_overall,
            }

        evidence_pack = self._phase2_fuse(evidence_items)
        raw_citations, lawyer_citations = self._phase3_citations(evidence_pack)
        context = self._phase4_context(evidence_pack)

        # ----- Prompt + LLM（流式）-----
        prompt = build_litigation_prompt(
            key, facts,
            parties=parties, claims=claims, court=court,
            evidence_context=context,
        )
        logger.info("write_litigation prompt: %s chars", len(prompt))

        t = time.perf_counter()
        if on_delta is None:
            document = chat_completion(prompt, max_tokens=8000)
        else:
            document = chat_completion_stream(prompt, on_delta=on_delta, max_tokens=8000)
        timing["llm_s"] = time.perf_counter() - t

        # ----- 引用一致性校验（warning 不阻断）-----
        citation_warnings = check_citations(document, evidence_pack)
        final = (document or "").strip()
        if citation_warnings:
            final += (
                "\n\n---\n"
                "⚠️ 引用校验提示（未在本次检索证据中核实，请人工复核）：\n"
                + "\n".join(f"  • {w}" for w in citation_warnings)
            )

        result: Dict[str, Any] = {
            "ok": True,
            "doc_type": key,
            "doc_label": DOC_TEMPLATES[key]["label"],
            "document": document,
            "final": final,
            "citation_warnings": citation_warnings,
            "citations": lawyer_citations,
            "timing_s": timing,
            "total_s": time.perf_counter() - t_overall,
        }
        logger.info(
            "write_litigation done | doc=%s chars | warnings=%s | total=%.2fs",
            len(final), len(citation_warnings), result["total_s"],
        )
        return result

    # ------------------------------------------------------------------
    # review_contract：合同审查（文件解析 → 法条检索 → 风险清单 → 引用校验）
    # ------------------------------------------------------------------

    def review_contract(
        self,
        file_path: str,
        *,
        contract_type: str = "",
        focus_points: str = "",
        top_k: int = 6,
        on_delta: DeltaCallback = None,
    ) -> Dict[str, Any]:
        """合同审查管线：文件 → 文本提取 → 法条检索 → 风险清单 prompt → LLM → 引用校验。

        与 write_litigation 同构：不跑 answer_guard，引用来源 warning 不阻断。
        """
        from core.contract_review import (
            build_review_prompt,
            build_review_query,
            extract_text,
            resolve_path,
            validate_path,
        )
        from core.litigation_builder import check_citations

        t_overall = time.perf_counter()
        timing: Dict[str, float] = {}

        try:
            resolved_path = resolve_path(file_path)
        except ValueError as exc:
            logger.warning("review_contract 路径解析失败: %s", exc)
            return {
                "ok": False,
                "report": "",
                "final": f"合同文件路径解析失败：{exc}",
                "citation_warnings": [],
                "citations": [],
                "timing_s": {},
                "total_s": time.perf_counter() - t_overall,
            }

        ok, err = validate_path(resolved_path)
        if not ok:
            logger.warning("review_contract 校验拒绝 | file=%s | %s", resolved_path, err)
            return {
                "ok": False,
                "report": "",
                "final": f"合同文件校验失败：{err}",
                "citation_warnings": [],
                "citations": [],
                "timing_s": {},
                "total_s": time.perf_counter() - t_overall,
            }

        try:
            contract_text, meta = extract_text(resolved_path)
        except ValueError as exc:
            return {
                "ok": False,
                "report": "",
                "final": f"合同文本提取失败：{exc}",
                "citation_warnings": [],
                "citations": [],
                "timing_s": {},
                "total_s": time.perf_counter() - t_overall,
            }

        logger.info(
            "Pipeline.review_contract | file=%s | %s chars | top_k=%s",
            meta["file"], meta["chars"], top_k,
        )

        # ----- 检索（复用 P1-P4）-----
        query = build_review_query(contract_text, contract_type, focus_points)
        t = time.perf_counter()
        evidence_items, ret_timing = self._phase1_retrieve(query, top_k)
        timing["phase1_retrieve_s"] = time.perf_counter() - t
        timing.update({f"retrieve_{k}": v for k, v in ret_timing.items()})

        evidence_pack = self._phase2_fuse(evidence_items) if evidence_items else []
        raw_citations, lawyer_citations = self._phase3_citations(evidence_pack)
        context = self._phase4_context(evidence_pack)

        # ----- Prompt + LLM（流式）-----
        prompt = build_review_prompt(
            contract_text,
            contract_type=contract_type,
            focus_points=focus_points,
            truncated=bool(meta.get("truncated")),
            evidence_context=context,
        )
        logger.info("review_contract prompt: %s chars", len(prompt))

        t = time.perf_counter()
        if on_delta is None:
            report = chat_completion(prompt, max_tokens=8000)
        else:
            report = chat_completion_stream(prompt, on_delta=on_delta, max_tokens=8000)
        timing["llm_s"] = time.perf_counter() - t

        # ----- 引用一致性校验（warning 不阻断）-----
        citation_warnings = (
            check_citations(report, evidence_pack) if evidence_pack
            else ["本次检索未命中法条，报告未经知识库校验。"]
        )
        final = (report or "").strip()
        if citation_warnings:
            final += (
                "\n\n---\n"
                "⚠️ 引用校验提示（未在本次检索证据中核实，请人工复核）：\n"
                + "\n".join(f"  • {w}" for w in citation_warnings)
            )

        result: Dict[str, Any] = {
            "ok": True,
            "report": report,
            "final": final,
            "file_meta": meta,
            "citation_warnings": citation_warnings,
            "citations": lawyer_citations,
            "timing_s": timing,
            "total_s": time.perf_counter() - t_overall,
        }
        logger.info(
            "review_contract done | report=%s chars | warnings=%s | total=%.2fs",
            len(final), len(citation_warnings), result["total_s"],
        )
        return result

    # ------------------------------------------------------------------
    # evaluate_evidence：诉讼证据评估（三性分析 + 证明力排序 + 质证建议）
    # ------------------------------------------------------------------

    def evaluate_evidence(
        self,
        evidence_text: str,
        *,
        case_facts: str = "",
        claims: str = "",
        top_k: int = 6,
        on_delta: DeltaCallback = None,
    ) -> Dict[str, Any]:
        """证据评估管线：证据材料 → 法条检索（证据规则）→ 评估 prompt → LLM → 引用校验。"""
        from core.evidence_eval import build_eval_prompt, build_eval_query
        from core.litigation_builder import check_citations

        t_overall = time.perf_counter()
        timing: Dict[str, float] = {}

        ev = (evidence_text or "").strip()
        if not ev:
            return {
                "ok": False,
                "report": "",
                "final": "证据材料为空。请提供证据清单（evidence_text，含名称/内容/来源）后重试。",
                "citation_warnings": [],
                "citations": [],
                "timing_s": {},
                "total_s": time.perf_counter() - t_overall,
            }

        logger.info("Pipeline.evaluate_evidence | evidence=%s chars | top_k=%s", len(ev), top_k)

        query = build_eval_query(ev, case_facts, claims)
        t = time.perf_counter()
        evidence_items, ret_timing = self._phase1_retrieve(query, top_k)
        timing["phase1_retrieve_s"] = time.perf_counter() - t
        timing.update({f"retrieve_{k}": v for k, v in ret_timing.items()})

        evidence_pack = self._phase2_fuse(evidence_items) if evidence_items else []
        raw_citations, lawyer_citations = self._phase3_citations(evidence_pack)
        context = self._phase4_context(evidence_pack)

        prompt = build_eval_prompt(
            ev, case_facts=case_facts, claims=claims, evidence_context=context,
        )
        logger.info("evaluate_evidence prompt: %s chars", len(prompt))

        t = time.perf_counter()
        if on_delta is None:
            report = chat_completion(prompt, max_tokens=8000)
        else:
            report = chat_completion_stream(prompt, on_delta=on_delta, max_tokens=8000)
        timing["llm_s"] = time.perf_counter() - t

        citation_warnings = (
            check_citations(report, evidence_pack) if evidence_pack
            else ["本次检索未命中法条，报告未经知识库校验。"]
        )
        final = (report or "").strip()
        if citation_warnings:
            final += (
                "\n\n---\n"
                "⚠️ 引用校验提示（未在本次检索证据中核实，请人工复核）：\n"
                + "\n".join(f"  • {w}" for w in citation_warnings)
            )

        result: Dict[str, Any] = {
            "ok": True,
            "report": report,
            "final": final,
            "citation_warnings": citation_warnings,
            "citations": lawyer_citations,
            "timing_s": timing,
            "total_s": time.perf_counter() - t_overall,
        }
        logger.info(
            "evaluate_evidence done | report=%s chars | warnings=%s | total=%.2fs",
            len(final), len(citation_warnings), result["total_s"],
        )
        return result


# ==========================================================================
# 模块级默认 Pipeline（懒加载）
# ==========================================================================

_DEFAULT_PIPELINE: Optional[LegalPipeline] = None


def get_pipeline() -> LegalPipeline:
    global _DEFAULT_PIPELINE
    if _DEFAULT_PIPELINE is None:
        _DEFAULT_PIPELINE = LegalPipeline()
    return _DEFAULT_PIPELINE
