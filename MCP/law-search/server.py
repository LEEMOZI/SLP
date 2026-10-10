"""Local legal-library MCP server.

All diagnostics go to ``logs/server.log`` and standard error.  Do not use
standard output for diagnostic messages: this server communicates with MCP
clients over standard input/output.

架构（RAG-12.11 统一检索层 + Pipeline）：
    search_law(question, top_k)
            ↓  直接转发到 core.pipeline.LegalPipeline.search_law()
            ↓    (Retrieve → Fusion → Citations → Context → Prompt → LLM → Guard)
    final answer string
"""

import os

os.environ["TOKENIZERS_PARALLELISM"] = "false"

import asyncio
import logging
import logging.handlers
import signal
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from fastmcp import FastMCP, Context

BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "logs" / "server.log"


def configure_logger() -> logging.Logger:
    """Send diagnostics somewhere safe for an stdio MCP server."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("law-search")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.addHandler(stderr_handler)

    return logger


logger = configure_logger()
logger.info("Starting legal MCP server")

mcp = FastMCP("law-search")

# P1-1: 输入长度上限 — 防止 LLM 传入超长文本导致 OOM / context window 溢出
_MAX_INPUT_CHARS = 50_000

# P1-4: pipeline 硬超时 — 短于 DSH toolCallTimeoutMs(240s)，防止 LM Studio swap 抖动时卡 10 分钟
_PIPELINE_TIMEOUT_SEC = 200


# Pipeline 懒加载（首次 search_law 调用时才加载 Chroma / Embedding / Reranker / LLM）
# _PIPELINE_LOCK：预热线程与工具调用可能并发触发构造，加锁防止模型双重加载
_PIPELINE = None
_PIPELINE_LOCK = threading.Lock()


def _get_pipeline():
    global _PIPELINE
    if _PIPELINE is None:
        with _PIPELINE_LOCK:
            if _PIPELINE is None:
                started = time.perf_counter()
                logger.info("Initializing LegalPipeline (lazy first load)...")
                # 延迟 import：避免 server.py 模块加载时就触发 chroma/transformer/llm 初始化
                from core.pipeline import LegalPipeline

                _PIPELINE = LegalPipeline()
                logger.info(
                    "LegalPipeline initialized in %.2fs",
                    time.perf_counter() - started,
                )
    return _PIPELINE


def _warmup_pipeline() -> None:
    """后台预热：DSH 启动即 spawn 本 server 并常驻，把 embedding/reranker/Chroma
    的冷加载（实测 ~3 分钟）挪到启动后台完成，用户首次 search_law 免付冷启动。
    线程内静默失败不影响主服务（懒加载仍是兜底）。"""

    def _run():
        try:
            started = time.perf_counter()
            pipeline = _get_pipeline()
            pipeline.warmup()
            logger.info(
                "Pipeline warmup finished in %.2fs", time.perf_counter() - started
            )
        except Exception as exc:  # noqa: BLE001 — 预热失败仅记录，不阻断服务
            logger.warning("Pipeline warmup skipped/failed: %s", exc)

    threading.Thread(target=_run, name="pipeline-warmup", daemon=True).start()


def _make_stream_reporter(ctx: Context, started: float):
    """把 LLM 流式增量转成 MCP progress/log 推送（S4）。

    - 回调运行在 worker 线程，用 run_coroutine_threadsafe 桥接回事件循环
    - 客户端未订阅 progressToken 时 report_progress 按 MCP 规范自动 no-op
    - 首个 token 推送 TTFT 日志；之后按「1.5s 或 400 字」节流推送进度
    """
    loop = asyncio.get_running_loop()
    state = {
        "chars": 0,
        "last_push_at": 0.0,
        "last_push_chars": 0,
        "ttft_sent": False,
    }

    def _consume(fut):
        exc = fut.exception()
        if exc:
            logger.debug("ctx push failed: %s", exc)

    def _fire(coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, loop).add_done_callback(_consume)

    def on_delta(piece: str) -> None:
        state["chars"] += len(piece or "")
        now = time.perf_counter()

        if not state["ttft_sent"]:
            state["ttft_sent"] = True
            state["last_push_at"] = now
            state["last_push_chars"] = state["chars"]
            _fire(ctx.info(f"流式生成开始 | 首 token {now - started:.1f}s"))
            return

        if (
            now - state["last_push_at"] >= 1.5
            or state["chars"] - state["last_push_chars"] >= 400
        ):
            state["last_push_at"] = now
            state["last_push_chars"] = state["chars"]
            _fire(ctx.report_progress(
                float(state["chars"]),
                message=f"已生成 {state['chars']} 字",
            ))

    return on_delta


@mcp.tool()
async def search_law(
    question: str,
    top_k: int = 8,
    ctx: Context = None,
) -> str:
    """【第一层·法律检索唯一入口·正常 99% 问题只需调用 1 次】检索本地中国法律法规、司法解释、资料（Chroma 向量库，免费，最快）。

    ## 🔴 工具调用铁律（违反会导致循环调用 + 5 分钟超时，必须严格执行）

    1) 【本工具调用次数上限 = 1 次】相同或近似关键词严禁第二次调用 search_law。

    2) 【返回值第 1 行已含 STOP SIGNAL + 法条原文表格】本工具返回值最开头就是：
          === 【AGENT LOOP · MANDATORY STOP SIGNAL】===
          +  【完整法条原文 Markdown 表格（法名 + 条文号 + 原文内容）】
       正常情况下直接依据该表格生成答案即可，完全不需要再去其他工具补充法条原文。

    3) 【绝对禁止在本工具调用后直接调用以下工具】：

       · ❌ chinese-law 系列全部 8 个工具（全部禁止直接调用）：
            search_legislation / get_provision / list_sources / validate_citation /
            build_legal_stance / format_citation / check_currency / about
         【chinese-law 仅作为应急兜底，不是默认第二层链路】
         允许调用 chinese-law 任何工具必须同时满足 3 条（AND 关系）：
           a) search_law 返回值的 STOP SIGNAL 明确写了『未命中任何相关法条』，
              或法条原文表格内容与用户问题完全不沾边；
              （注意：『内容不够详细 / 不够全面』≠ 不相关，不属于允许理由）
           b) 用户在当前轮对话明确提出要使用 chinese-law，或明确需要一部表格中完全不存在的偏门法；
           c) 同一用户问题下，chinese-law 系列工具此前从未被调用过（每个问题限 1 次）。
         以上 3 条缺任何一条，都严禁调用 chinese-law 的任何工具。

       · ❌ search_case：除非用户在问题中明确出现「找案例」「判决书」「类似案件」「指导案例」等字样，
            否则严禁调用（法条依据类问答不需要案例）。

       · ❌ search_external（Tavily，消耗免费配额）：严禁用于查找已知法律的具体条文（这是 search_law 的职责）；
            仅在 search_law + chinese-law（如获用户同意）都明确无结果，或用户询问最新动态/新闻时，才允许调用 1 次。

    4) 【累计工具调用总次数上限】：含本条 search_law 在内，每个用户问题最多 3 次工具调用；
       到 3 次必须立刻停止一切工具调用，用已有信息直接生成答案。

    ## ✅ 标准调用流程（99% 场景只走第一步）

       1 次 search_law("用户的法律问题") → 读返回值开头的 STOP SIGNAL + 法条表格 → 直接输出最终答案。

    仅在 STOP SIGNAL 明确写「未命中」或表格完全不相关时，才需要先与用户确认是否允许调用兜底工具。
    """
    started = time.perf_counter()
    clean_question = (question or "").strip()
    if not clean_question:
        logger.warning("Rejected an empty search request")
        return "查询内容为空。请提供需要检索的法律问题或关键词。"
    if len(clean_question) > _MAX_INPUT_CHARS:
        logger.warning("Rejected oversized search request | len=%s", len(clean_question))
        return f"查询内容过长（{len(clean_question)} 字），请精简至 {_MAX_INPUT_CHARS} 字以内。"

    logger.info("Tool call received | question=%r | top_k=%s", clean_question, top_k)

    try:
        pipeline = _get_pipeline()
        on_delta = _make_stream_reporter(ctx, started) if ctx is not None else None
        final_answer = await asyncio.wait_for(
            asyncio.to_thread(
                pipeline.search_law,
                clean_question,
                top_k=top_k,
                on_delta=on_delta,
            ),
            timeout=_PIPELINE_TIMEOUT_SEC,
        )
        logger.info(
            "search_law finished | chars=%s | total=%.2fs",
            len(final_answer), time.perf_counter() - started,
        )
        return final_answer

    except asyncio.TimeoutError:
        logger.error("search_law timed out after %ss", _PIPELINE_TIMEOUT_SEC)
        return (
            f"本地法律检索超时（{_PIPELINE_TIMEOUT_SEC}s）。"
            "可能原因：LM Studio 模型加载中 / 内存不足 / 网络延迟。请稍后重试。"
        )
    except Exception:
        logger.exception("search_law failed after %.2fs", time.perf_counter() - started)
        return (
            "本地法律检索暂时无法完成。请查看本地日志 "
            f"{LOG_PATH} 以定位错误。"
        )


# ---------------------------------------------------------------------------
# write_litigation：法律文书起草（检索法条 → 模板 → LLM → 引用校验）
# ---------------------------------------------------------------------------


@mcp.tool()
async def write_litigation(
    doc_type: str,
    case_facts: str,
    parties: str = "",
    claims: str = "",
    court: str = "",
    top_k: int = 8,
    ctx: Context = None,
) -> str:
    """起草中国法律文书（起诉状/答辩状/代理词/法律意见书）。

    内部调用 search_law 检索法条依据（第一层），结果不足时你可补充调用
    search_legislation（@ansvar）或 pkulaw-citation-validator 验证引用。

    文中引用的法条均经来源一致性校验，未经核实的引用会在文末以警告块标出。

    Args:
        doc_type: 文书类型 — complaint(起诉状) / defense(答辩状) /
            representation(代理词) / legal_opinion(法律意见书)，中文亦可
        case_facts: 案情事实描述（必填，按时间线陈述，尽量具体）
        parties: 当事人信息（原告/被告姓名名称、住址、联系方式等，可空）
        claims: 诉讼请求/答辩要点/代理意见要点（可空，缺省由模型归纳）
        court: 受诉法院或致送对象名称（可空）
        top_k: 检索法条数量（默认 8）
    """
    started = time.perf_counter()

    for _field, _val in [("case_facts", case_facts), ("parties", parties), ("claims", claims)]:
        if _val and len(_val) > _MAX_INPUT_CHARS:
            return f"{_field} 内容过长（{len(_val)} 字），请精简至 {_MAX_INPUT_CHARS} 字以内。"

    try:
        pipeline = _get_pipeline()
        on_delta = _make_stream_reporter(ctx, started) if ctx is not None else None
        result = await asyncio.to_thread(
            pipeline.write_litigation,
            doc_type,
            case_facts or "",
            parties=parties or "",
            claims=claims or "",
            court=court or "",
            top_k=top_k,
            on_delta=on_delta,
        )
        logger.info(
            "write_litigation finished | ok=%s | doc_type=%s | chars=%s | total=%.2fs",
            result["ok"], result["doc_type"], len(result["final"]),
            time.perf_counter() - started,
        )
        return result["final"]

    except Exception:
        logger.exception(
            "write_litigation failed after %.2fs", time.perf_counter() - started
        )
        return (
            "文书起草暂时无法完成。请查看本地日志 "
            f"{LOG_PATH} 以定位错误。"
        )


# ---------------------------------------------------------------------------
# review_contract：合同审查（文件解析 → 法条检索 → 风险清单 → 引用校验）
# ---------------------------------------------------------------------------


@mcp.tool()
async def review_contract(
    file_path: str,
    contract_type: str = "",
    focus_points: str = "",
    top_k: int = 6,
    ctx: Context = None,
) -> str:
    """审查合同文件，输出风险清单报告。

    支持格式：PDF / Word(.docx) / txt / md / 图片(png·jpg 等扫描件，
    自动 OCR 识别，逐页上限 50 页)。上限 20MB，仅限已授权目录：
    /Users/simon/Documents/1-Work、law-search 项目目录、以及
    DSH 上传区 /Users/simon/Documents/1-SLP/DSH WorkSpace/.dsh-uploads。

    路径说明：用户在对话中上传的文件实际存放于 DSH 上传区的 session 子目录
    （文件名带 16 位 hash 前缀）。若传入的是原始路径/原始文件名且文件已不在
    原位置，工具会自动到上传区按文件名匹配，无需精确路径。

    内部调用 search_law 检索法条依据（第一层），结果不足时你可补充调用
    search_legislation（@ansvar）获取更全面条文。

    Args:
        file_path: 合同文件绝对路径（必填）；上传区相对路径
            （.dsh-uploads/...）或原始文件路径均可，自动解析
        contract_type: 合同类型，如 买卖/租赁/劳动/借款（可空，自动判断）
        focus_points: 重点关注条款或问题（可空）
        top_k: 检索法条数量（默认 6）
    """
    started = time.perf_counter()

    try:
        pipeline = _get_pipeline()
        on_delta = _make_stream_reporter(ctx, started) if ctx is not None else None
        result = await asyncio.to_thread(
            pipeline.review_contract,
            file_path or "",
            contract_type=contract_type or "",
            focus_points=focus_points or "",
            top_k=top_k,
            on_delta=on_delta,
        )
        logger.info(
            "review_contract finished | ok=%s | file=%s | chars=%s | total=%.2fs",
            result["ok"], file_path, len(result["final"]),
            time.perf_counter() - started,
        )
        return result["final"]

    except Exception:
        logger.exception(
            "review_contract failed after %.2fs", time.perf_counter() - started
        )
        return (
            "合同审查暂时无法完成。请查看本地日志 "
            f"{LOG_PATH} 以定位错误。"
        )


# ---------------------------------------------------------------------------
# evaluate_evidence：诉讼证据评估（三性分析 → 证明力排序 → 质证建议）
# ---------------------------------------------------------------------------


@mcp.tool()
async def evaluate_evidence(
    evidence_text: str,
    case_facts: str = "",
    claims: str = "",
    top_k: int = 6,
    ctx: Context = None,
) -> str:
    """评估诉讼证据材料，输出三性分析与证明力报告。

    内部调用 search_law 检索证据相关法条（第一层）。

    对当事人提交的证据（书证/物证/证人证言/电子数据等）逐项分析
    真实性、合法性、关联性，按证明力排序，并给出质证要点与补强建议。
    报告为诉讼策略参考，不构成对证据效力的司法认定。

    Args:
        evidence_text: 证据材料清单（必填，逐项列明名称、内容、来源）
        case_facts: 案情背景（可空，帮助关联性判断）
        claims: 待证事实 / 诉讼主张（可空）
        top_k: 检索法条数量（默认 6）
    """
    started = time.perf_counter()

    for _field, _val in [("evidence_text", evidence_text), ("case_facts", case_facts), ("claims", claims)]:
        if _val and len(_val) > _MAX_INPUT_CHARS:
            return f"{_field} 内容过长（{len(_val)} 字），请精简至 {_MAX_INPUT_CHARS} 字以内。"

    try:
        pipeline = _get_pipeline()
        on_delta = _make_stream_reporter(ctx, started) if ctx is not None else None
        result = await asyncio.to_thread(
            pipeline.evaluate_evidence,
            evidence_text or "",
            case_facts=case_facts or "",
            claims=claims or "",
            top_k=top_k,
            on_delta=on_delta,
        )
        logger.info(
            "evaluate_evidence finished | ok=%s | chars=%s | total=%.2fs",
            result["ok"], len(result["final"]), time.perf_counter() - started,
        )
        return result["final"]

    except Exception:
        logger.exception(
            "evaluate_evidence failed after %.2fs", time.perf_counter() - started
        )
        return (
            "证据评估暂时无法完成。请查看本地日志 "
            f"{LOG_PATH} 以定位错误。"
        )


# ---------------------------------------------------------------------------
# search_case：案例检索（case_library，指导性案例等）——与 search_law 独立
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_case(question: str, top_k: int = 6) -> str:
    """【第三层·条件触发·非默认】检索本地收录的指导性案例与案例资料（Chroma 向量库）。

    ## 🔴 调用条件（不满足严禁调用）
    - 仅限用户在问题中明确提出："找案例""查判决书""类似案件""司法判例""指导案例" 等关键词时调用；
    - 一般法律问答（问法条依据、法律适用、法律解释）严禁调用本工具，必须只用 search_law。

    ## 🔴 次数约束
    - 每个用户问题最多调用【1 次】，严禁换关键词反复重试；
    - 与 search_law / search_external 合计，累计工具调用不得超过 3 次（到 3 次强制停）。

    仅当本工具返回结果不足、且用户仍需要更多案例时，才依次尝试：
      → pkulaw-case-semantic-search / pkulaw-case-keyword（北大法宝案例库）
      → wk-mcp（威科先行综合检索）
      → search_external（Tavily 全网搜索，末位兜底，仅限 1 次）
    """
    started = time.perf_counter()
    clean_question = (question or "").strip()
    if not clean_question:
        return "查询内容为空。请提供需要检索的案例问题或关键词。"

    try:
        from core.retrieval_service import get_retriever

        items, _timing = await asyncio.to_thread(
            get_retriever().search_cases, clean_question, top_k=max(1, min(top_k, 20)),
        )
        if not items:
            return (
                "本地案例库暂未检索到与该问题相关的案例。"
                "可尝试调整关键词，或改用 search_law 检索法条依据。"
            )

        lines = [f"共检索到 {len(items)} 条相关案例（按相关性排序）：", ""]
        for i, item in enumerate(items, 1):
            meta = item.get("metadata") or {}
            title = (item.get("title") or meta.get("document_title")
                     or meta.get("filename") or "未命名案例")
            page = item.get("page") or meta.get("page_number") or ""
            if page in ("None", None, ""):
                page = ""
            score = item.get("rerank_score") or 0
            text = (item.get("content") or item.get("document") or "").strip()
            lines.append(f"【案例 {i}】{title}" + (f"（第 {page} 页）" if page else ""))
            lines.append(f"相关度: {float(score):.3f}")
            lines.append(text[:600] + ("…" if len(text) > 600 else ""))
            lines.append("")

        logger.info(
            "search_case finished | question=%r | hits=%s | total=%.2fs",
            clean_question, len(items), time.perf_counter() - started,
        )
        return "\n".join(lines)

    except Exception:
        logger.exception("search_case failed after %.2fs", time.perf_counter() - started)
        return (
            "案例检索暂时无法完成。请查看本地日志 "
            f"{LOG_PATH} 以定位错误。"
        )


# ---------------------------------------------------------------------------
# chroma_status（P2-3 最小版：只读诊断，不加载 Embedding / Reranker / LLM）
# ---------------------------------------------------------------------------

_CHROMA_ADMIN_CLIENT = None


def _get_chroma_admin_client():
    """懒加载一个只读用途的 Chroma PersistentClient（不触发模型加载）。"""
    global _CHROMA_ADMIN_CLIENT
    if _CHROMA_ADMIN_CLIENT is None:
        import chromadb

        from config import CHROMA_PATH

        _CHROMA_ADMIN_CLIENT = chromadb.PersistentClient(path=str(CHROMA_PATH))
    return _CHROMA_ADMIN_CLIENT


@mcp.tool()
def chroma_status() -> str:
    """查看本地法律知识库（Chroma 向量库）的运行状态。

    返回 JSON 字符串，包含：
    - collections: 每个集合的 chunk 数量
    - total_chunks: 全部集合 chunk 总数
    - law_name_fill_rate: law_name 元数据字段填充率（0~1）
    - last_ingest_snapshot: 最近一次 ingest 后自动快照（文件名 / 时间 / 大小MB）
    - hash_index_mtime: 增量去重索引最后修改时间

    只读操作，不加载模型，适合在调用 search_law 前自检知识库是否可用。
    """
    import json as _json
    from datetime import datetime

    result: dict = {
        "ok": False,
        "chroma_path": None,
        "collections": [],
        "total_chunks": 0,
        "law_name_fill_rate": None,
        "last_ingest_snapshot": None,
        "hash_index_mtime": None,
        "errors": [],
    }

    try:
        from config import CHROMA_PATH

        result["chroma_path"] = str(CHROMA_PATH)
    except Exception as exc:
        result["errors"].append(f"config import failed: {exc!r}")
        return _json.dumps(result, ensure_ascii=False, indent=2)

    # 1) 集合清单 + chunk 数 + law_name 填充率
    try:
        client = _get_chroma_admin_client()
        collections = client.list_collections()
        total = 0
        filled = 0
        seen = 0
        for col in collections:
            try:
                count = col.count()
            except Exception as exc:
                result["errors"].append(f"count({col.name}) failed: {exc!r}")
                count = -1
            result["collections"].append({"name": col.name, "chunks": count})
            if count > 0:
                total += count
                try:
                    metas = (col.get(include=["metadatas"]) or {}).get("metadatas") or []
                    for meta in metas:
                        seen += 1
                        if isinstance(meta, dict) and str(meta.get("law_name") or "").strip():
                            filled += 1
                except Exception as exc:
                    result["errors"].append(f"get({col.name}) failed: {exc!r}")
        result["total_chunks"] = total
        if seen:
            result["law_name_fill_rate"] = round(filled / seen, 4)
    except Exception as exc:
        result["errors"].append(f"chroma inspect failed: {exc!r}")

    # 2) 最近 ingest 快照（P1-3 chroma_lock 的产出）
    try:
        snap_dir = BASE_DIR / "chroma_snapshots"
        if snap_dir.is_dir():
            snaps = sorted(snap_dir.glob("*.tar.gz"), reverse=True)
            if snaps:
                latest = snaps[0]
                stat = latest.stat()
                result["last_ingest_snapshot"] = {
                    "file": latest.name,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "size_mb": round(stat.st_size / 1024 / 1024, 1),
                }
    except Exception as exc:
        result["errors"].append(f"snapshot inspect failed: {exc!r}")

    # 3) hash index 最后修改时间
    try:
        from config import HASH_INDEX_FILE

        p = Path(HASH_INDEX_FILE)
        if p.exists():
            result["hash_index_mtime"] = datetime.fromtimestamp(
                p.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as exc:
        result["errors"].append(f"hash index inspect failed: {exc!r}")

    result["ok"] = result["total_chunks"] > 0

    logger.info(
        "chroma_status | ok=%s | chunks=%s | fill_rate=%s | errors=%s",
        result["ok"], result["total_chunks"], result["law_name_fill_rate"],
        len(result["errors"]),
    )
    return _json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def ingest_rebuild(
    libraries: Optional[List[str]] = None,
    limit: Optional[int] = None,
    confirm: bool = False,
) -> str:
    """重建/增量更新本地法律知识库（封装 ingest.py）。

    默认 dry-run（只扫描不写入），confirm=True 才真正写入。
    写入由 core.chroma_lock 的进程写锁保护，完成后自动打快照（保留 3 份）。

    Args:
        libraries: 要处理的知识库名列表；None 表示全部。
        limit: 限制处理文件数量（测试用）。
        confirm: False=dry-run 扫描；True=实际写入（可能耗时数十分钟）。
    """
    import json as _json
    import subprocess
    import sys as _sys

    cmd = [_sys.executable, str(BASE_DIR / "ingest.py")]
    if libraries:
        cmd += ["--library", *[str(x) for x in libraries]]
    if limit:
        cmd += ["--limit", str(int(limit))]
    if not confirm:
        cmd += ["--dry-run"]

    timeout_sec = 7200 if confirm else 600
    result: dict = {
        "ok": False,
        "mode": "write" if confirm else "dry_run",
        "cmd": cmd,
        "timeout_sec": timeout_sec,
        "returncode": None,
        "output_tail": "",
        "error": None,
    }

    logger.info("ingest_rebuild start | mode=%s | libraries=%s | limit=%s",
                result["mode"], libraries, limit)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=str(BASE_DIR),
        )
        result["returncode"] = proc.returncode
        tail = (proc.stdout or "").splitlines()[-40:]
        err_tail = (proc.stderr or "").splitlines()[-10:]
        result["output_tail"] = "\n".join(tail + (["[stderr]"] + err_tail if err_tail else []))
        result["ok"] = proc.returncode == 0
    except subprocess.TimeoutExpired:
        result["error"] = f"ingest 超过 {timeout_sec}s 未完成，进程已终止"
    except Exception as exc:  # pragma: no cover
        result["error"] = repr(exc)

    logger.info("ingest_rebuild done | ok=%s | rc=%s", result["ok"], result["returncode"])
    return _json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def search_external(
    question: str,
    max_results: int = 5,
    topic: str = "general",
) -> str:
    """【末位兜底·绝对最后调用·消耗 Tavily 免费配额】Tavily Web Search（1,000 次/月免费额度）。

    ## 🔴 严禁调用的场景（违反会立刻花光免费配额 + 超时）
    - ❌ 禁止用于"查某部已知法律的具体条文"（例如《公司法》第X条内容）——这是 search_law 的职责；
    - ❌ 禁止在 search_law 刚执行完就自动调用；只有 search_law 明确写了"未命中任何相关法条"且用户仍不满意时，才可以调用；
    - ❌ 禁止换关键词反复重试；每个用户问题最多调用【1 次】。

    ## ⚠️ 允许调用的唯一前置条件
    必须同时满足：
      (1) search_law（已调用 1 次）返回空结果或内容完全不相关；
      (2) search_legislation（@ansvar chinese-law，如可用）也未命中；
      (3) pkulaw-law-search / wk-mcp（如可用）也未命中；
          → 以上全部失效，或用户明确询问"最新动态/新闻/时事" → 才允许调用本工具【1 次】。

    ## 🔴 累计次数上限
    与 search_law / search_case 合计，累计工具调用不得超过 3 次；到 3 次必须立刻停止一切工具调用。

    Args:
        question: 搜索关键词或问题。
        max_results: 最多返回结果数（默认 5，免费额度下建议 ≤5）。
        topic: "general"（通用）或 "news"（仅限最新法规动态/新闻类查询）。

    Returns:
        JSON 字符串，包含搜索结果文本和元数据。
    """
    import json as _json
    from core.web_search import web_search

    text, meta = web_search(
        query=question,
        max_results=max_results,
        topic=topic,
    )
    result = {"answer_text": text, "metadata": meta}
    logger.info("search_external | query=%s | results=%s", question, meta.get("result_count", 0))
    return _json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# read_document：通用文档解析（PDF / Office / 图片 / 纯文本 → 纯文本）
# ---------------------------------------------------------------------------

@mcp.tool()
def read_document(file_path: str, max_chars: int = 100_000) -> str:
    """读取文档文件并返回提取的纯文本内容。

    支持 PDF（含扫描件 OCR）、Word(.doc/.docx)、PowerPoint(.ppt/.pptx)、
    Excel(.xls/.xlsx)、HTML(.html/.htm)、图片(.png/.jpg 等，OCR 识别)、
    纯文本(.txt/.md)。

    Args:
        file_path: 文件绝对路径（必填）。仅限已授权目录：
            /Users/simon/Documents/1-Work 与 law-search/uploads。
        max_chars: 返回文本的最大字符数（默认 100,000，超出截断）。
    """
    import json as _json
    from core.contract_review import extract_text

    started = time.perf_counter()
    path = (file_path or "").strip()

    if not path:
        return "文件路径不能为空。"

    # 授权目录检查（与 contract_review.ALLOWED_ROOTS 保持一致）
    from core.contract_review import ALLOWED_ROOTS as _CONTRACT_ALLOWED_ROOTS
    real_path = os.path.realpath(os.path.expanduser(path))
    if not any(real_path.startswith(r + os.sep) or real_path == r for r in _CONTRACT_ALLOWED_ROOTS):
        return (
            f"文件路径不在授权目录内（{real_path}）。"
            f"仅限：{', '.join(_CONTRACT_ALLOWED_ROOTS)}。"
        )

    if not os.path.isfile(real_path):
        return f"文件不存在：{real_path}"

    try:
        text, meta = extract_text(real_path)
        meta["file"] = real_path
        meta["chars"] = len(text)

        # 按调用方指定的 max_chars 截断
        if len(text) > max_chars:
            meta["truncated"] = True
            meta["orig_chars"] = len(text)
            text = text[:max_chars]

        result = {"text": text, "metadata": meta}
        logger.info(
            "read_document | file=%s | chars=%s | truncated=%s | %.2fs",
            real_path, len(text), meta.get("truncated", False),
            time.perf_counter() - started,
        )
        return _json.dumps(result, ensure_ascii=False, indent=2)

    except ValueError as exc:
        logger.warning("read_document failed | file=%s | error=%s", real_path, exc)
        return f"文档解析失败：{exc}"
    except Exception as exc:
        logger.exception("read_document error | file=%s", real_path)
        return f"文档解析出错：{exc}"


if __name__ == "__main__":
    # transport 由启动参数决定：
    #   stdio（默认）：MCP client 子进程挂载（Bionic / LM Studio / harness registry）
    #   http：Streamable HTTP（/mcp 端点），dsh-mcp-client 原生支持的远程 transport
    #        （config: transport=streamable-http + url），供新主机迁移后 DSH 远程接入。
    #   sse：HTTP+SSE 旧协议（/sse 端点），兼容早期远程消费方。
    #
    # 鉴权（--auth-token）：
    #   提供 Bearer Token 鉴权中间件，适用于公网/内网暴露场景。
    #   客户端请求须携带 Header: Authorization: Bearer <token>
    #   未携带或 token 不匹配 → 401 Unauthorized
    import argparse
    import secrets

    parser = argparse.ArgumentParser(description="law-search MCP server")
    parser.add_argument(
        "--transport", choices=["stdio", "http", "sse"], default="stdio",
        help="MCP transport（默认 stdio；http=streamable HTTP / sse=SSE 供远程 client 接入）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP/SSE 监听地址")
    parser.add_argument("--port", type=int, default=8765, help="HTTP/SSE 监听端口")
    parser.add_argument(
        "--auth-token", "--token", dest="auth_token", default="",
        help="Bearer Token 鉴权密钥（空=不启用鉴权；可传固定字符串或 'auto' 自动生成）；短别名 --token 与 run_cloud.sh / 文档一致",
    )
    parser.add_argument(
        "--auth-token-file", "--token-file", dest="auth_token_file", default="",
        help="将 token 写入指定文件（方便客户端读取）；短别名 --token-file",
    )
    args = parser.parse_args()

    # ---- Bearer Token 鉴权中间件 ----
    actual_token = ""
    if args.auth_token and args.transport in ("http", "sse"):
        # 'auto' → 随机生成 32 字节 hex token
        if args.auth_token == "auto":
            actual_token = secrets.token_hex(32)
            logger.info("鉴权 token 已自动生成（32 字节 hex）")
        else:
            actual_token = args.auth_token

        # 写入 token 文件（方便客户端脚本读取）
        if args.auth_token_file:
            token_path = Path(args.auth_token_file)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(actual_token, encoding="utf-8")
            logger.info("鉴权 token 已写入: %s", args.auth_token_file)

        logger.info(
            "Bearer Token 鉴权已就绪（transport=%s, token_len=%s）。所有非 PUBLIC_PATHS 请求必须带 Header: Authorization: Bearer <token>，否则返回 401。",
            args.transport, len(actual_token),
        )
    elif args.auth_token and args.transport == "stdio":
        logger.warning("--auth-token 在 stdio transport 下无效，已忽略（stdio 由父进程挂载，不走 HTTP）。")

    # Bearer Token ASGI 鉴权中间件（http / sse 共享，避免两份实现分叉）
    def _build_wrapped_app(asgi_app, token: str, lifespan):
        """把裸 ASGI app 包装成 Starlette + BearerTokenAuthMiddleware。

        ⚠️ FastMCP 3.4.5 ASGI 集成铁律（修复 2026-09-03 RuntimeError 'Task group is not initialized'）：
           fastmcp http_app() 返回的 ASGI app 自带 lifespan（内部启动 SessionManager
           task group）。若包到外层 Starlette 时不把 lifespan 透传给父 app，
           uvicorn 只会执行父 app 的空 startup，task group 永远处于未初始化状态，
           任何通过鉴权后的 /mcp 请求都会 RuntimeError 500。

           解决：Starlette() 构造时显式传 `lifespan=asgi_app_lifespan`。

        另外在外层 Starlette 注册 /health /healthz / 三个公开路由（无需鉴权），
        修掉 fastmcp http_app 不自带 /health 造成的 404 误导。
        """
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse, PlainTextResponse
        from starlette.applications import Starlette
        from starlette.routing import Route, Mount

        _token = token  # closure

        # ---------- 公开健康检查路由（无鉴权，在外层）----------
        async def _health(_request):
            return JSONResponse(
                {"ok": True, "service": "law-search-mcp", "transport": args.transport}
            )

        async def _root(_request):
            msg = (
                "law-search MCP server\n"
                f"- transport: {args.transport}\n"
                "- endpoints: /mcp (streamable HTTP, default) | /sse (if enabled)\n"
                "- auth:      Bearer Token required for all non-health paths\n"
            )
            return PlainTextResponse(msg)

        # ---------- 鉴权中间件（仍然通过 PUBLIC_PATHS 放行公开路由，保持一致语义）----------
        PUBLIC_PATHS = frozenset({"/health", "/healthz", "/", "/favicon.ico"})

        class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path in PUBLIC_PATHS:
                    return await call_next(request)
                auth_header = request.headers.get("Authorization", "")
                if not auth_header.startswith("Bearer "):
                    return JSONResponse(
                        {"error": "Missing or invalid Authorization header"},
                        status_code=401,
                    )
                provided = auth_header[7:]
                if not secrets.compare_digest(provided, _token):
                    return JSONResponse(
                        {"error": "Invalid bearer token"},
                        status_code=401,
                    )
                return await call_next(request)

        wrapped = Starlette(
            # 关键：把 fastmcp http_app 内部的 lifespan 透传给父 Starlette
            lifespan=lifespan,
            routes=[
                Route("/", endpoint=_root),
                Route("/health", endpoint=_health),
                Route("/healthz", endpoint=_health),
                # fastmcp http_app 内部已经路由了 /mcp /sse 等，这里挂载成 /
                Mount("/", app=asgi_app),
            ],
        )
        wrapped.add_middleware(BearerTokenAuthMiddleware)
        return wrapped

    # 常驻场景（DSH stdio / HTTP 远程）：启动即后台预热检索层，
    # 用户首次 search_law 免付 ~3 分钟冷启动。失败不影响主服务。
    _warmup_pipeline()

    # http / sse 统一走 uvicorn：两边都能透明挂 Bearer Token 鉴权中间件，
    # 不再走 mcp.run() 的内置 HTTP server——后者无法插入自定义 ASGI 中间件，
    # 会导致 --auth-token 在 http 下被静默忽略（S1 鉴权绕过漏洞）。
    import uvicorn

    if args.transport == "http":
        mcp_app = mcp.http_app(transport="http")
        endpoint = f"http://{args.host}:{args.port}/mcp"
        if actual_token:
            # 注意：lifespan 必须显式传入外层 Starlette，否则 SessionManager task group
            # 不会初始化 → RuntimeError 500（2026-09-03 实测坑点）
            asgi_app = _build_wrapped_app(mcp_app, actual_token, lifespan=mcp_app.lifespan)
            logger.info("HTTP(streamable) + Bearer Token 鉴权 | uvicorn 启动 | 端点: %s", endpoint)
        else:
            asgi_app = mcp_app
            logger.info("HTTP(streamable) | uvicorn 启动（无鉴权，仅限 loopback） | 端点: %s", endpoint)
        uvicorn.run(asgi_app, host=args.host, port=args.port)

    elif args.transport == "sse":
        mcp_app = mcp.http_app(transport="sse")
        endpoint = f"http://{args.host}:{args.port}/sse"
        if actual_token:
            asgi_app = _build_wrapped_app(mcp_app, actual_token, lifespan=mcp_app.lifespan)
            logger.info("SSE + Bearer Token 鉴权 | uvicorn 启动 | 端点: %s", endpoint)
        else:
            asgi_app = mcp_app
            logger.info("SSE | uvicorn 启动（无鉴权，仅限 loopback） | 端点: %s", endpoint)
        uvicorn.run(asgi_app, host=args.host, port=args.port)
    else:
        # P1-5: 优雅关闭 — SIGTERM/SIGINT 时记录日志再退出，
        # 便于 DSH/launchd 重启时排查关闭原因（chroma 写锁由 lock 机制保护）
        def _graceful_shutdown(signum, _frame):
            sig_name = signal.Signals(signum).name
            logger.info("Received %s — shutting down gracefully...", sig_name)
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _graceful_shutdown)
        signal.signal(signal.SIGINT, _graceful_shutdown)
        logger.info("Legal MCP server ready; transport is managed by the MCP client")
        mcp.run()
