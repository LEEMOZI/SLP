"""P1：Agent 运行观测库。

记录每次 LLM 调用和关键工具调用的 provider、token、耗时、fallback 与交付
状态。写入失败只记 debug 日志，不影响主流程。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("law-search.observability")

_DB_PATH = Path(
    os.environ.get(
        "LAW_SEARCH_OBS_DB",
        str(Path(__file__).resolve().parents[1] / "data" / "agent_runs.db"),
    )
)

_LOCK = threading.Lock()
_INITIALIZED = False


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _LOCK:
        if _INITIALIZED:
            return
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'llm',
                    case_id TEXT,
                    client TEXT,
                    provider TEXT,
                    model TEXT,
                    prompt_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    total_ms REAL DEFAULT 0,
                    ttft_ms REAL,
                    status TEXT,
                    task_type TEXT,
                    tool TEXT,
                    delivery_status TEXT,
                    error TEXT,
                    extra TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_created "
                "ON agent_runs(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_case "
                "ON agent_runs(case_id)"
            )
        _INITIALIZED = True


def record_run(
    *,
    kind: str = "llm",
    case_id: Optional[str] = None,
    client: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    total_ms: float = 0,
    ttft_ms: Optional[float] = None,
    status: str = "ok",
    task_type: Optional[str] = None,
    tool: Optional[str] = None,
    delivery_status: Optional[str] = None,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """写入一条运行记录；任何异常都吞掉。"""
    try:
        _init()
        with _LOCK:
            with _connect() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_runs (
                        created_at, kind, case_id, client, provider, model,
                        prompt_tokens, output_tokens, total_ms, ttft_ms,
                        status, task_type, tool, delivery_status, error, extra
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        kind,
                        case_id,
                        client,
                        provider,
                        model,
                        int(prompt_tokens or 0),
                        int(output_tokens or 0),
                        float(total_ms or 0),
                        float(ttft_ms) if ttft_ms is not None else None,
                        status,
                        task_type,
                        tool,
                        delivery_status,
                        error,
                        json.dumps(extra or {}, ensure_ascii=False),
                    ),
                )
    except Exception:
        logger.debug("Observability write failed", exc_info=True)


def record_tool(
    *,
    tool: str,
    status: str = "ok",
    case_id: Optional[str] = None,
    total_ms: float = 0,
    delivery_status: Optional[str] = None,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    record_run(
        kind="tool",
        tool=tool,
        status=status,
        case_id=case_id,
        total_ms=total_ms,
        delivery_status=delivery_status,
        error=error,
        extra=extra,
    )


def summarize(days: int = 7) -> Dict[str, Any]:
    """给报表用的聚合。"""
    _init()
    since = time.time() - max(1, days) * 86400
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_runs WHERE created_at >= ? ORDER BY created_at",
            (since,),
        ).fetchall()

    llm = [r for r in rows if r["kind"] == "llm"]
    tools = [r for r in rows if r["kind"] == "tool"]
    map_reduce = [r for r in rows if r["kind"] == "map_reduce"]
    ok = [r for r in llm if r["status"] == "ok"]
    durations = sorted(r["total_ms"] or 0 for r in llm)

    def p95(values):
        if not values:
            return 0
        idx = min(len(values) - 1, int(round(len(values) * 0.95)) - 1)
        return values[max(0, idx)]

    providers: Dict[str, int] = {}
    for r in llm:
        key = r["provider"] or "unknown"
        providers[key] = providers.get(key, 0) + 1

    return {
        "days": days,
        "db": str(_DB_PATH),
        "llm_calls": len(llm),
        "llm_ok": len(ok),
        "llm_success_rate": round(len(ok) / len(llm), 4) if llm else None,
        "llm_p95_ms": round(p95(durations), 1),
        "llm_max_ms": round(max(durations), 1) if durations else 0,
        "llm_providers": providers,
        "tool_calls": len(tools),
        "map_reduce_calls": len(map_reduce),
        "map_reduce_degraded": sum(
            1 for r in map_reduce if r["status"] == "degraded"
        ),
        "delivery_blocked": sum(
            1 for r in tools if r["delivery_status"] == "blocked"
        ),
        "delivery_ok": sum(
            1 for r in tools if r["delivery_status"] == "delivered"
        ),
        "rows": len(rows),
    }
