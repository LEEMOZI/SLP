"""LM Studio 模型驻留控制。

64 GB 统一内存下，qwen/qwen3.6-35b-a3b 与 qwen/qwen3.8-27b 无法同时以
128K 常驻：LM Studio 的内存护栏会直接拒绝加载第二个模型（HTTP 400，
约 0.02 秒返回），并不会自动换出已驻留模型。

因此请求质量模型（27B）前，必须显式卸载当前驻留的 LLM，再加载目标模型。
本模块用 `lms` CLI 完成卸载/加载，供 LMStudioLocalProvider 在
auto_swap=True 时调用。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LM_STUDIO_API_BASE = os.environ.get(
    "LM_STUDIO_API_BASE",
    "http://127.0.0.1:1234",
)

LMS_BIN = Path(
    os.environ.get(
        "LMS_BIN",
        str(Path.home() / ".cache/lm-studio/bin/lms"),
    )
)

SWAP_TIMEOUT_SEC = int(os.environ.get("LM_STUDIO_SWAP_TIMEOUT_SEC", "300"))
PARALLEL = int(os.environ.get("LM_STUDIO_PARALLEL", "4"))
TTL_SEC = int(os.environ.get("LM_STUDIO_TTL_SEC", "86400"))

# 单模型加载档案：LM Studio 按「权重 + KV cache」估算峰值内存，超限直接拒绝加载。
# Gemma 4 31B 在 128K 上下文 × 4 并行下需约 54.8 GB，64 GB 机器必然被拒；
# 实测 64K × 2 并行仅占 17.6 GB，可正常加载。ttl 让备用模型空闲后自动卸载，
# 好让健康检查脚本恢复默认的 qwen/qwen3.6-35b-a3b。
MODEL_PROFILES: Dict[str, Dict[str, int]] = {
    "google/gemma-4-31b-qat": {"context": 65536, "parallel": 2, "ttl": 1800},
}

_SWAP_LOCK = threading.Lock()


def _request_json(path: str, timeout: float = 10.0) -> Dict[str, Any]:
    with urllib.request.urlopen(
        LM_STUDIO_API_BASE + path, timeout=timeout
    ) as resp:
        return json.loads(resp.read().decode())


def base_model_id(model_id: str) -> str:
    """去掉 LM Studio 的实例后缀（qwen/xxx:2 → qwen/xxx）。"""
    return (model_id or "").split(":", 1)[0]


def list_loaded_llms() -> List[Dict[str, Any]]:
    """当前已加载 / 加载中的 LLM、VLM（排除 embedding）。"""
    data = _request_json("/api/v0/models")
    return [
        item
        for item in data.get("data", [])
        if item.get("type") != "embeddings"
        and item.get("state") in ("loaded", "loading")
    ]


def _lms(*args: str, timeout: Optional[int] = None) -> None:
    if not LMS_BIN.exists():
        raise RuntimeError(f"lms CLI 不存在: {LMS_BIN}")
    proc = subprocess.run(
        [str(LMS_BIN), *args],
        capture_output=True,
        text=True,
        timeout=timeout or SWAP_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(f"lms {' '.join(args)} 失败: {detail}")


def _unload(model_id: str) -> None:
    logger.info("LM Studio swap | unload %s", model_id)
    try:
        _lms("unload", model_id, timeout=60)
    except Exception as exc:
        logger.warning("LM Studio swap | unload %s failed: %s", model_id, exc)


def _load(
    model_id: str,
    context_length: int,
    parallel: int = PARALLEL,
    ttl: int = TTL_SEC,
) -> None:
    logger.info(
        "LM Studio swap | load %s @ %s ctx, parallel=%s（约 10s）",
        model_id,
        context_length,
        parallel,
    )
    _lms(
        "load",
        model_id,
        "-c",
        str(context_length),
        "--gpu",
        "max",
        "--parallel",
        str(parallel),
        "--ttl",
        str(ttl),
        "-y",
        timeout=SWAP_TIMEOUT_SEC,
    )


def _load_with_retry(
    model_id: str,
    context_length: int,
    parallel: int,
    ttl: int,
) -> None:
    """内存守护栏拒绝时，降一档上下文/并行再试一次。"""
    try:
        _load(model_id, context_length, parallel, ttl)
        return
    except RuntimeError as exc:
        if "insufficient system resources" not in str(exc).lower():
            raise
        retry_ctx = max(8192, context_length // 2)
        logger.warning(
            "LM Studio swap | %s 在 %s ctx 内存不足，降级到 %s ctx / parallel=1",
            model_id,
            context_length,
            retry_ctx,
        )
        _load(model_id, retry_ctx, 1, ttl)


def ensure_model_loaded(model: str, context_length: int) -> bool:
    """确保 model 以 >= context_length 驻留；必要时卸载其它 LLM 腾内存。

    Returns:
        True  → 发生过换模（本次调用会额外耗时约 10s）
        False → 目标模型已按足够上下文驻留，无需换模
    """
    target = base_model_id(model)

    profile = MODEL_PROFILES.get(target, {})
    max_context = int(profile.get("context") or 0)
    if max_context and context_length > max_context:
        logger.info(
            "LM Studio swap | %s 档案上限 %s ctx（请求 %s）",
            target,
            max_context,
            context_length,
        )
        context_length = max_context
    parallel = int(profile.get("parallel") or PARALLEL)
    ttl = int(profile.get("ttl") or TTL_SEC)

    with _SWAP_LOCK:
        loaded = list_loaded_llms()

        for item in loaded:
            if base_model_id(item.get("id", "")) != target:
                continue
            loaded_ctx = int(item.get("loaded_context_length") or 0)
            if loaded_ctx >= context_length:
                return False
            logger.info(
                "LM Studio swap | %s 上下文 %s < %s，重载",
                item.get("id"),
                loaded_ctx,
                context_length,
            )
            _unload(item["id"])
            _load_with_retry(target, context_length, parallel, ttl)
            return True

        for item in loaded:
            _unload(item["id"])

        _load_with_retry(target, context_length, parallel, ttl)
        return True
