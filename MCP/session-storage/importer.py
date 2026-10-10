# -*- coding: utf-8 -*-
"""
session-storage.importer
========================

DSH 会话导入器：把 DSH Desktop 的本地会话文件增量导入 SQLite。

数据源（2026-09-02 实测格式）：
  1. harness/sessions/<workspace-slug>/session-<uuid>/session.jsonl.zstd
     每行一个 JSON：session 头 / user/message / assistant/message /
     assistant/chunk / session/title / turn|step 边界等（带 seq+time）。
     assistant 内容块类型：text / reasoning / tool-call
     （tool-call: {"id", "name": "mcp__server__tool", "arguments": "<json str>"}）
  2. harness/storages/session_projcache/sessions/session-<uuid>.json
     未压缩投影缓存：title / turns / tokens / model / lastPromptAt 等元数据，
     优先作为索引元数据（无需解压 zstd）。

增量策略：zstd 文件 mtime 与库内记录一致 → 跳过；不一致 → 整会话重导。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import zstandard as zstd

from db import connect, init_db, session_write_lock

logger = logging.getLogger("session-storage.importer")

DSH_SESSIONS_DIR = os.path.expanduser(
    "~/Library/Application Support/dsh-desktop/harness/sessions"
)
PROJCACHE_DIR = os.path.expanduser(
    "~/Library/Application Support/dsh-desktop/harness/storages/session_projcache/sessions"
)

# reasoning 块是内部思维链，不入库（省空间且无检索价值）
_SKIP_BLOCK_TYPES = {"reasoning"}


# ==========================================================================
# 解析
# ==========================================================================

def _parse_zstd(zstd_path: str) -> Dict[str, Any]:
    """解压并解析一个会话文件，返回结构化中间结果。"""
    dctx = zstd.ZstdDecompressor()
    with open(zstd_path, "rb") as f:
        text = dctx.stream_reader(f).read().decode("utf-8", "replace")

    header: Dict[str, Any] = {}
    title: str = ""
    model_sel: Dict[str, str] = {}
    messages: List[Dict[str, Any]] = []

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue  # 损坏行跳过，不影响其余
        ltype = obj.get("type")

        if ltype == "session":
            header = {
                "session_id": obj.get("id"),
                "cwd": obj.get("cwd"),
                "created_at": obj.get("createdAt"),
            }
        elif ltype == "session/title":
            t = (obj.get("data") or {}).get("title")
            if t:
                title = t
        elif ltype == "model/selection":
            d = obj.get("data") or {}
            if d.get("provider"):
                model_sel = {"provider": d.get("provider"), "model": d.get("model")}
        elif ltype in ("user/message", "assistant/message"):
            data = obj.get("data") or {}
            msg = data.get("message") or data  # user/message 的 data 本身即消息体
            role = msg.get("role") or ("user" if ltype == "user/message" else "assistant")
            turn = data.get("turn", 0)
            step = data.get("step", 0)
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype in _SKIP_BLOCK_TYPES:
                    continue
                if btype == "text":
                    body = block.get("text") or ""
                    if body.strip():
                        messages.append({
                            "seq": obj.get("seq"),
                            "role": role,
                            "turn": turn,
                            "step": step,
                            "content_type": "text",
                            "content": body,
                            "tool_name": None,
                            "time": obj.get("time"),
                        })
                elif btype == "tool-call":
                    messages.append({
                        "seq": obj.get("seq"),
                        "role": "assistant",
                        "turn": turn,
                        "step": step,
                        "content_type": "tool-call",
                        "content": block.get("arguments") or "{}",
                        "tool_name": block.get("name"),
                        "time": obj.get("time"),
                    })

    return {
        "header": header,
        "title": title,
        "model": model_sel,
        "messages": messages,
    }


def _parse_projcache(session_id: str) -> Dict[str, Any]:
    """读取投影缓存元数据（失败静默返回空——projcache 只是增强，不是必需）。"""
    pc_path = os.path.join(PROJCACHE_DIR, f"{session_id}.json")
    out: Dict[str, Any] = {}
    if not os.path.isfile(pc_path):
        return out
    try:
        with open(pc_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        rows = (obj.get("record") or {}).get("rows") or {}
        stats = (rows.get("sessionStats") or {}).get("val") or {}
        title_row = (rows.get("title") or {}).get("val")
        token = (rows.get("tokenUsage") or {}).get("val") or {}
        totals = token.get("totals") or {}
        model = ((rows.get("modelSelection") or {}).get("val") or {}).get("lastUsed") or {}
        meta = (rows.get("sessionListMetadata") or {}).get("val") or {}
        out = {
            "title": title_row if isinstance(title_row, str) else None,
            "turns": stats.get("turns"),
            "steps": stats.get("steps"),
            "llm_ms": stats.get("llmMs"),
            "tool_ms": stats.get("toolMs"),
            "input_tokens": totals.get("uncachedInputTokens"),
            "output_tokens": totals.get("outputTokens"),
            "provider": model.get("provider"),
            "model": model.get("model"),
            "last_prompt_at": meta.get("lastPromptAt"),
        }
        out = {k: v for k, v in out.items() if v is not None}
    except Exception as exc:
        logger.warning("projcache 解析失败 %s: %s", pc_path, exc)
    return out


# ==========================================================================
# 增量导入
# ==========================================================================

def sync_sessions() -> Dict[str, Any]:
    """扫描 DSH 会话目录并增量导入。返回统计信息。"""
    init_db()
    zstd_files = sorted(glob.glob(
        os.path.join(DSH_SESSIONS_DIR, "*", "session-*", "session.jsonl.zstd")
    ))
    if not zstd_files:
        return {"total": 0, "imported": 0, "skipped": 0, "failed": 0,
                "message": f"未找到会话文件（{DSH_SESSIONS_DIR}）"}

    conn = connect()
    stats = {"total": len(zstd_files), "imported": 0, "skipped": 0, "failed": 0}
    try:
        # 已导入会话的 mtime 索引
        known = {
            r["session_id"]: r["zstd_mtime"]
            for r in conn.execute("SELECT session_id, zstd_mtime FROM sessions")
        }

        for zstd_path in zstd_files:
            mtime = int(os.path.getmtime(zstd_path) * 1000)
            parts = zstd_path.split(os.sep)
            workspace = parts[-3] if len(parts) >= 3 else ""
            session_id = parts[-2] if len(parts) >= 2 else ""

            if session_id in known and known[session_id] == mtime:
                stats["skipped"] += 1
                continue

            try:
                parsed = _parse_zstd(zstd_path)
                meta = _parse_projcache(session_id)
                header = parsed["header"]
                if not header.get("session_id"):
                    header["session_id"] = session_id

                title = meta.get("title") or parsed["title"] or "(无标题)"
                provider = meta.get("provider") or parsed["model"].get("provider")
                model = meta.get("model") or parsed["model"].get("model")

                with session_write_lock():
                    conn.execute("BEGIN")
                    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
                    conn.execute(
                        """INSERT OR REPLACE INTO sessions
                           (session_id, workspace, cwd, title, created_at, last_prompt_at,
                            turns, steps, provider, model, input_tokens, output_tokens,
                            llm_ms, tool_ms, source, zstd_mtime, zstd_path, imported_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'import',?,?,?)""",
                        (
                            session_id, workspace, header.get("cwd"), title,
                            header.get("created_at"), meta.get("last_prompt_at"),
                            meta.get("turns"), meta.get("steps"),
                            provider, model,
                            meta.get("input_tokens"), meta.get("output_tokens"),
                            meta.get("llm_ms"), meta.get("tool_ms"),
                            mtime, zstd_path, int(time.time() * 1000),
                        ),
                    )
                    for m in parsed["messages"]:
                        cur = conn.execute(
                            """INSERT INTO messages
                               (session_id, seq, role, turn, step, content_type,
                                content, tool_name, time)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (session_id, m["seq"], m["role"], m["turn"], m["step"],
                             m["content_type"], m["content"], m["tool_name"], m["time"]),
                        )
                        if m["content_type"] == "text":
                            conn.execute(
                                "INSERT INTO messages_fts (content, session_id, message_id) VALUES (?,?,?)",
                                (m["content"], session_id, cur.lastrowid),
                            )
                    conn.execute("COMMIT")
                stats["imported"] += 1
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                stats["failed"] += 1
                logger.exception("导入失败 %s: %s", zstd_path, exc)
    finally:
        conn.close()
    return stats


def load_session_messages(session_id: str, include_tool_calls: bool = False,
                          limit: int = 200) -> List[Dict[str, Any]]:
    """按 seq 顺序读出一个会话的全部消息。"""
    conn = connect()
    try:
        sql = "SELECT seq, role, turn, step, content_type, content, tool_name, time FROM messages WHERE session_id = ?"
        if not include_tool_calls:
            sql += " AND content_type = 'text'"
        sql += " ORDER BY seq LIMIT ?"
        rows = conn.execute(sql, (session_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
