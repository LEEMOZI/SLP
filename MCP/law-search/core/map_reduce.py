"""P1：超长 prompt 的 map-reduce 分段执行。

触发条件：prompt 估算 token 超过 ``AUTO_ROUTE["chunk_prompt_tokens"]``
（默认 400K）。此时单请求即使云端 1M 上下文也会极慢且容易超时，改为：

    split   : 按行/段落边界切分，尽量不把法条、判决段落从中间截断
    map     : 每段独立抽取要点（保留案号、日期、金额、当事人、法条编号、原文）
    reduce  : 合并要点生成最终答案；要点仍过长则递归 map 再 reduce

只在 ``core.llm_client`` 的 Router 里自动调用，本模块不依赖任何 Provider，
调用方通过 ``call`` 回调注入（便于测试与替换）。
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from config import (
    MAP_REDUCE_CHUNK_TOKENS,
    MAP_REDUCE_MAX_WORKERS,
    MAP_REDUCE_REDUCE_INPUT_TOKENS,
    TOKEN_ESTIMATE_SAFETY_FACTOR,
)

logger = logging.getLogger("law-search.map_reduce")

# 单段目标 token 数。48K 让本地 128K 也能吃下单段，云端 prefill 也保持在秒级。
CHUNK_TOKENS = int(
    os.environ.get("LAW_SEARCH_MAP_CHUNK_TOKENS", str(MAP_REDUCE_CHUNK_TOKENS))
)

# reduce 输入上限：合并后的要点超过它就再切一轮（递归 map）。
REDUCE_INPUT_TOKENS = int(
    os.environ.get(
        "LAW_SEARCH_MAP_REDUCE_INPUT_TOKENS",
        str(MAP_REDUCE_REDUCE_INPUT_TOKENS),
    )
)

# map 并发数。云端 API 可并发；本地 LM Studio parallel=4，同样适合 4。
MAX_WORKERS = max(1, int(os.environ.get("LAW_SEARCH_MAP_WORKERS", str(MAP_REDUCE_MAX_WORKERS))))

# reduce 时从原 prompt 开头截取的“任务背景”长度（字符）。
HEADER_CHARS = max(500, int(os.environ.get("LAW_SEARCH_MAP_HEADER_CHARS", "4000")))

# 递归 reduce 的最大层数，防止要点反复压缩不收敛。
MAX_LEVELS = 3

_CJK_WEIGHT = 0.5
_OTHER_WEIGHT = 0.25
_BREAKERS = "。！？；!?;\n"

MAP_TEMPLATE = """你在分段处理一份超长材料（第 {i}/{n} 段）。请只做事实抽取，不做结论推断。

【任务背景（材料开头，仅供理解任务）】
{header}

【材料第 {i}/{n} 段】
{chunk}

请输出与任务相关的要点（中文，条目式）：
1. 保留全部案号、日期、金额、当事人名称、法条编号、页码或段落位置；
2. 引用关键原文时用「」标注，不要改写；
3. 不要跨段推断，不要输出与任务无关的泛泛结论；
4. 本段没有相关信息时，只输出「本段无相关信息」。"""

REDUCE_TEMPLATE = """【原任务】
{header}

【材料分段要点（共 {n} 段，已按原文顺序合并）】
{notes}

请基于以上要点完成原任务，要求：
1. 只使用要点中出现的事实，不得编造案号、日期、金额、当事人；
2. 若要点缺少完成任务所需的关键事实，明确写「材料未提及」，不要猜测；
3. 输出结构完整、可直接交付的最终结果。"""

DEGRADED_FOOTER = (
    "\n\n> ⚠️ 提示：本次为超长材料分段处理，部分分段抽取失败，"
    "结论可能不完整，建议核对原文。"
)


@dataclass
class MapReduceResult:
    text: str
    chunks: int
    levels: int
    degraded: bool
    elapsed_s: float
    prompt_tokens: int


def estimate_tokens(text: str) -> int:
    """与 llm_client 相同的保守估算：中文 2 字符/token，其他 4 字符/token，
    再乘实测校准系数（中文法律文本实际约 1.47 字符/token）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    non_cjk = len(text) - cjk
    raw = cjk / 2 + non_cjk / 4
    return max(1, int(raw * TOKEN_ESTIMATE_SAFETY_FACTOR))


def _weight_tokens(text: str) -> float:
    """不取整的 token 权重，用于切分时累计，避免逐行取整后越界。"""
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    non_cjk = len(text) - cjk
    raw = cjk * _CJK_WEIGHT + non_cjk * _OTHER_WEIGHT
    return raw * TOKEN_ESTIMATE_SAFETY_FACTOR


def _hard_split(text: str, max_tokens: int) -> List[str]:
    """单行/单段本身超限时，按字符权重硬切，优先在标点处断开。"""
    pieces: List[str] = []
    start = 0
    i = 0
    tokens = 0.0
    n = len(text)

    while i < n:
        ch = text[i]
        per_char = _CJK_WEIGHT if "\u4e00" <= ch <= "\u9fff" else _OTHER_WEIGHT
        tokens += per_char * TOKEN_ESTIMATE_SAFETY_FACTOR
        i += 1
        if tokens < max_tokens:
            continue

        cut = i
        window_start = max(start + 1, i - max(1, int((i - start) * 0.25)))
        for j in range(i - 1, window_start - 1, -1):
            if text[j] in _BREAKERS:
                cut = j + 1
                break

        if cut <= start:
            cut = i
        pieces.append(text[start:cut])
        start = cut
        i = cut
        tokens = 0.0

    if start < n:
        pieces.append(text[start:])

    return pieces


def split_text(text: str, max_tokens: int) -> List[str]:
    """按行聚合切分；单行超限时硬切。返回按原顺序排列的非空分段。"""
    if not text:
        return []
    if max_tokens <= 0 or estimate_tokens(text) <= max_tokens:
        return [text]

    chunks: List[str] = []
    buffer: List[str] = []
    buffer_tokens = 0

    def _flush() -> None:
        nonlocal buffer, buffer_tokens
        piece = "".join(buffer)
        if piece:
            chunks.append(piece)
        buffer = []
        buffer_tokens = 0

    for line in text.splitlines(keepends=True):
        line_tokens = _weight_tokens(line)

        if line_tokens > max_tokens:
            _flush()
            chunks.extend(_hard_split(line, max_tokens))
            continue

        if buffer and buffer_tokens + line_tokens > max_tokens:
            _flush()

        buffer.append(line)
        buffer_tokens += line_tokens

    _flush()
    return chunks


def _call_with_retry(call: Callable[..., str], prompt: str, **kwargs) -> str:
    """map 阶段单段失败重试一次，减少长任务整体失败概率。"""
    last_exc: Optional[BaseException] = None
    for attempt in (1, 2):
        try:
            return call(prompt, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 由调用方决定降级策略
            last_exc = exc
            logger.warning(
                "Map chunk attempt failed | attempt=%s/2 | error=%s",
                attempt,
                type(exc).__name__,
            )
            if attempt == 1:
                time.sleep(1)
    raise last_exc  # type: ignore[misc]


def _map_chunks(
    chunks: List[str],
    *,
    header: str,
    call: Callable[..., str],
    call_kwargs: dict,
    level: int,
) -> tuple[List[str], bool]:
    """并发执行 map，返回（要点列表, 是否降级）。顺序与 chunks 一致。"""
    total = len(chunks)
    degraded = False

    def _one(index: int) -> str:
        chunk = chunks[index]
        prompt = MAP_TEMPLATE.format(
            i=index + 1,
            n=total,
            header=header or "（未提供）",
            chunk=chunk,
        )
        try:
            return _call_with_retry(call, prompt, **call_kwargs)
        except Exception as exc:  # noqa: BLE001 — 单段失败降级为原文截断
            logger.error(
                "Map chunk failed permanently | level=%s | chunk=%s/%s | error=%s",
                level,
                index + 1,
                total,
                type(exc).__name__,
            )
            return (
                f"【第 {index + 1}/{total} 段抽取失败，以下为原文截断】\n"
                f"{chunk[:2000]}"
            )

    workers = min(MAX_WORKERS, total)
    if workers <= 1:
        notes = [_one(i) for i in range(total)]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="map-reduce"
        ) as pool:
            notes = list(pool.map(_one, range(total)))

    degraded = any(
        note.startswith("【第 ") and "段抽取失败" in note[:40] for note in notes
    )
    return notes, degraded


def run_map_reduce(
    prompt: str,
    call: Callable[..., str],
    *,
    task_type_hint: Optional[str] = None,
    provider_hint: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    force_model: Optional[str] = None,
) -> MapReduceResult:
    """对超长 prompt 执行 map-reduce。

    ``call`` 必须是 ``call(prompt, **kwargs) -> str`` 的 LLM 调用函数；
    应当已经禁用 map-reduce，避免递归触发。
    """
    started = time.perf_counter()
    prompt_tokens = estimate_tokens(prompt)
    header = prompt[:HEADER_CHARS]

    call_kwargs = {
        "task_type_hint": task_type_hint,
        "provider_hint": provider_hint,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "force_model": force_model,
    }

    chunks = split_text(prompt, CHUNK_TOKENS)
    if len(chunks) <= 1:
        text = call(prompt, **call_kwargs)
        return MapReduceResult(
            text=text,
            chunks=1,
            levels=1,
            degraded=False,
            elapsed_s=time.perf_counter() - started,
            prompt_tokens=prompt_tokens,
        )

    levels = 1
    notes, degraded = _map_chunks(
        chunks, header=header, call=call, call_kwargs=call_kwargs, level=levels
    )
    notes_text = "\n\n".join(notes)

    # 要点仍超 reduce 上限：把要点再当材料切一轮（递归 map）。
    while estimate_tokens(notes_text) > REDUCE_INPUT_TOKENS and levels < MAX_LEVELS:
        levels += 1
        sub_chunks = split_text(notes_text, CHUNK_TOKENS)
        if len(sub_chunks) <= 1:
            break
        logger.info(
            "Map-reduce level %s | notes_tokens=%s | sub_chunks=%s",
            levels,
            estimate_tokens(notes_text),
            len(sub_chunks),
        )
        notes, sub_degraded = _map_chunks(
            sub_chunks,
            header=header,
            call=call,
            call_kwargs=call_kwargs,
            level=levels,
        )
        degraded = degraded or sub_degraded
        notes_text = "\n\n".join(notes)

    reduce_prompt = REDUCE_TEMPLATE.format(
        header=header or "（未提供）",
        n=len(notes),
        notes=notes_text,
    )

    text = call(reduce_prompt, **call_kwargs)
    if degraded and DEGRADED_FOOTER not in text:
        text += DEGRADED_FOOTER

    elapsed = time.perf_counter() - started
    logger.info(
        "Map-reduce done | prompt_tokens=%s | chunks=%s | levels=%s | "
        "degraded=%s | elapsed=%.1fs",
        prompt_tokens,
        len(chunks),
        levels,
        degraded,
        elapsed,
    )

    return MapReduceResult(
        text=text,
        chunks=len(chunks),
        levels=levels,
        degraded=degraded,
        elapsed_s=elapsed,
        prompt_tokens=prompt_tokens,
    )
