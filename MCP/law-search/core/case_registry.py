"""P0-2：案件会话登记。

一个 case_id 对应一个案件会话，默认 24 小时有效。SLP 不控制客户端的
聊天窗口，但通过交付工具上的 case_id 做服务端兜底：过期或未登记的
case_id 不允许正式交付。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import SESSION_TTL_HOURS

logger = logging.getLogger("law-search.cases")

_DB_PATH = Path(
    os.environ.get(
        "LAW_SEARCH_CASE_DB",
        str(Path(__file__).resolve().parents[1] / "data" / "cases.db"),
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
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    source_docs TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
        _INITIALIZED = True


def _normalize_sources(source_docs: Any) -> List[str]:
    if not source_docs:
        return []
    if isinstance(source_docs, str):
        raw = source_docs.replace("\n", ",").split(",")
    else:
        raw = list(source_docs)
    return [str(x).strip() for x in raw if str(x).strip()]


def open_case(
    case_id: str,
    source_docs: Any = None,
    ttl_hours: float = SESSION_TTL_HOURS,
) -> Dict[str, Any]:
    """登记或复用案件。过期案件不会自动续期，必须显式换 case_id。"""
    case_id = (case_id or "").strip()
    if not case_id:
        return {"ok": False, "status": "missing_case_id", "message": "case_id 不能为空"}

    _init()
    now = time.time()
    sources = _normalize_sources(source_docs)

    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()

            if row is None:
                expires_at = now + max(1.0, float(ttl_hours)) * 3600
                conn.execute(
                    """
                    INSERT INTO cases (
                        case_id, created_at, last_seen, expires_at, status, source_docs
                    ) VALUES (?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        case_id,
                        now,
                        now,
                        expires_at,
                        json.dumps(sources, ensure_ascii=False),
                    ),
                )
                return {
                    "ok": True,
                    "status": "created",
                    "case_id": case_id,
                    "expires_at": expires_at,
                    "source_docs": sources,
                }

            if row["status"] != "active" or now > float(row["expires_at"]):
                conn.execute(
                    "UPDATE cases SET status = 'expired' WHERE case_id = ?",
                    (case_id,),
                )
                return {
                    "ok": False,
                    "status": "expired",
                    "case_id": case_id,
                    "expires_at": row["expires_at"],
                    "message": "案件会话已过期，请新建 case_id 会话",
                }

            merged = sorted(
                set(json.loads(row["source_docs"] or "[]")) | set(sources)
            )
            conn.execute(
                """
                UPDATE cases
                SET last_seen = ?, source_docs = ?
                WHERE case_id = ?
                """,
                (now, json.dumps(merged, ensure_ascii=False), case_id),
            )
            return {
                "ok": True,
                "status": "active",
                "case_id": case_id,
                "expires_at": row["expires_at"],
                "source_docs": merged,
            }


def check_case(case_id: str) -> Optional[Dict[str, Any]]:
    """返回案件状态；不存在返回 None。过期会惰性标记。"""
    case_id = (case_id or "").strip()
    if not case_id:
        return None
    _init()
    now = time.time()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if row is None:
                return None
            status = row["status"]
            if status == "active" and now > float(row["expires_at"]):
                status = "expired"
                conn.execute(
                    "UPDATE cases SET status = 'expired' WHERE case_id = ?",
                    (case_id,),
                )
            return {
                "case_id": case_id,
                "status": status,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "source_docs": json.loads(row["source_docs"] or "[]"),
            }


def touch_case(case_id: str) -> None:
    case_id = (case_id or "").strip()
    if not case_id:
        return
    _init()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "UPDATE cases SET last_seen = ? WHERE case_id = ?",
                (time.time(), case_id),
            )
