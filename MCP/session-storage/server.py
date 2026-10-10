# -*- coding: utf-8 -*-
"""
session-storage MCP Server
==========================

DSH 本地会话的持久化与检索服务（P1 会话持久化层）。

把 DSH Desktop 的本地会话记录（session.jsonl.zstd + 投影缓存）增量导入
本地 SQLite（FTS5 中文全文检索），暴露 6 个工具：同步、检索、调阅、
打标签、导出、统计。纯本地存储，不联网——符合律师保密义务。

工具调用时机（写给 Agent）：
  - 法律咨询告一段落时：先 sync_sessions 同步本会话，再 tag_session 打标签归档
  - 用户问"之前讨论过 X 吗 / 上次的检索结果"：search_sessions → get_session
  - 用户要导出咨询记录：export_session（导出到 ~/Desktop/dsh_sessions/）
  - 不要重复 sync：sync_sessions 有 mtime 增量去重，重复调用无害但无意义
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from fastmcp import FastMCP

import db
from importer import load_session_messages, sync_sessions

# --------------------------------------------------------------------------
# 日志：stdio transport 下严禁污染 stdout，全部走 stderr
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("session-storage.server")

mcp = FastMCP("session-storage")

DEFAULT_EXPORT_DIR = os.path.expanduser("~/Desktop/dsh_sessions")


def _fmt_ms(ms: Optional[int]) -> str:
    if not ms:
        return "未知"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _session_brief(row) -> str:
    tags = f" [标签: {row['tags']}]" if row["tags"] else ""
    return (
        f"- {row['title']}（{_fmt_ms(row['created_at'])}，{row['turns'] or '?'} 轮，"
        f"{row['model'] or '未知模型'}）{tags} id={row['session_id']}"
    )


# ==========================================================================
# 工具 1：同步
# ==========================================================================

@mcp.tool
def sync_sessions_tool() -> str:
    """【第一步·同步】把 DSH 本地会话增量导入检索库。

    何时调用：
    - 法律咨询告一段落、需要归档本次咨询记录时
    - 用户要求"保存/归档/同步会话记录"时
    - 首次使用本服务时（一次导入全部历史）

    返回导入统计（imported=本次新导入 / skipped=mtime 未变跳过 / failed）。
    有 mtime 增量去重，重复调用无害。
    """
    stats = sync_sessions()
    lines = [f"同步完成：共 {stats['total']} 个会话文件，"
             f"新导入 {stats['imported']}，跳过 {stats['skipped']}，失败 {stats['failed']}"]
    if stats.get("message"):
        lines.append(stats["message"])
    return "\n".join(lines)


# ==========================================================================
# 工具 2：检索
# ==========================================================================

@mcp.tool
def search_sessions(query: str, limit: int = 10, tag: Optional[str] = None) -> str:
    """按关键词检索历史咨询会话（全文检索标题+对话内容）。

    何时调用：
    - 用户问"之前讨论过 XX 吗 / 上次那个问题 / 我们关于 X 的咨询"
    - 需要回顾某法律领域的历史意见时

    Args:
        query: 关键词（中文 ≥2 字；≥3 字走全文索引，2 字自动降级模糊匹配）
        limit: 最多返回条数（默认 10）
        tag: 可选，按标签过滤（如客户名、案件编号、公司法）
    """
    db.init_db()
    conn = db.connect()
    try:
        params: list = []
        where_extra = ""
        if tag:
            where_extra = " AND s.tags LIKE ?"
            params.append(f"%{tag}%")

        hit_ids: list = []
        if len(query.strip()) >= 3:
            # FTS5 短语包裹：防止 query 中的特殊字符（- < : " 等）导致语法错误
            phrase = '"' + query.strip().replace('"', '""') + '"'
            fts_rows = conn.execute(
                """SELECT session_id,
                          snippet(messages_fts, 0, '「', '」', '…', 12) AS frag
                   FROM messages_fts WHERE messages_fts MATCH ?
                   ORDER BY rowid LIMIT 200""",
                (phrase,),
            ).fetchall()
            # snippet 不能与 COUNT 聚合混用 → Python 侧聚合
            agg: dict = {}
            for r in fts_rows:
                sid = r["session_id"]
                if sid not in agg:
                    agg[sid] = {"hits": 0, "frag": r["frag"]}
                agg[sid]["hits"] += 1
            ranked = sorted(agg.items(), key=lambda kv: -kv[1]["hits"])[:50]
            hit_ids = [sid for sid, _ in ranked]
            frags = {sid: v["frag"] for sid, v in ranked}

        if not hit_ids:  # FTS 无命中或 2 字短词 → LIKE 降级
            like_rows = conn.execute(
                """SELECT m.session_id, COUNT(*) AS hits,
                          substr(m.content, max(1, instr(m.content, ?) - 40), 90) AS frag
                   FROM messages m
                   WHERE m.content LIKE ? ESCAPE '\\'
                   GROUP BY m.session_id ORDER BY hits DESC LIMIT 50""",
                (query, f"%{query}%"),
            ).fetchall()
            hit_ids = [r["session_id"] for r in like_rows]
            frags = {r["session_id"]: r["frag"] for r in like_rows}

        if not hit_ids:
            return f"未检索到包含「{query}」的历史会话。可尝试 sync_sessions 先同步。"

        placeholders = ",".join("?" * len(hit_ids))
        rows = conn.execute(
            f"""SELECT s.* FROM sessions s
                WHERE s.session_id IN ({placeholders}){where_extra}
                ORDER BY s.created_at DESC LIMIT ?""",
            (*hit_ids, *params, limit),
        ).fetchall()
    finally:
        conn.close()

    out = [f"检索「{query}」命中 {len(rows)} 个会话：", ""]
    for r in rows:
        out.append(_session_brief(r))
        frag = frags.get(r["session_id"])
        if frag:
            out.append(f"  片段: …{frag}…")
        out.append("")
    out.append("调阅完整对话用 get_session(session_id=...)。")
    return "\n".join(out)


# ==========================================================================
# 工具 3：调阅
# ==========================================================================

@mcp.tool
def get_session(session_id: str, include_tool_calls: bool = False,
                limit: int = 100) -> str:
    """调阅某个历史会话的完整对话内容（含当时的检索工具调用链）。

    何时调用：search_sessions 找到目标会话后，需要查看完整问答/证据链时。

    Args:
        session_id: 会话 ID（search_sessions 返回的 id）
        include_tool_calls: True 时同时返回工具调用记录（search_law 等检索了什么参数）
        limit: 最多返回消息条数（默认 100，长会话可调大）
    """
    db.init_db()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return f"会话 {session_id} 不在库中。先跑 sync_sessions_tool 同步。"

    msgs = load_session_messages(session_id, include_tool_calls=include_tool_calls, limit=limit)
    out = [
        f"会话: {row['title']}",
        f"时间: {_fmt_ms(row['created_at'])} | 轮次: {row['turns'] or '?'} | 模型: {row['model'] or '未知'}",
        f"标签: {row['tags'] or '无'} | 摘要: {row['summary'] or '无'}",
        f"消息数: {len(msgs)}（limit={limit}）",
        "=" * 50,
    ]
    for m in msgs:
        ts = _fmt_ms(m["time"])
        if m["content_type"] == "tool-call":
            args = m["content"]
            try:
                args = json.dumps(json.loads(args), ensure_ascii=False)
            except Exception:
                pass
            out.append(f"[{ts}] 🔧 {m['tool_name']}({args[:160]})")
        else:
            body = m["content"]
            if len(body) > 800:
                body = body[:800] + f"…（截断，全文 {len(m['content'])} 字符）"
            who = "用户" if m["role"] == "user" else "助手"
            out.append(f"[{ts}] {who}: {body}")
        out.append("")
    return "\n".join(out)


# ==========================================================================
# 工具 4：打标签
# ==========================================================================

@mcp.tool
def tag_session(session_id: str, tags: str, summary: str = "") -> str:
    """给会话打标签（客户名/案件编号/法律领域），可选写一句话摘要。

    何时调用：法律咨询结束时归档（先 sync_sessions_tool 再 tag_session）。
    标签用于后续 search_sessions(tag=...) 按客户/案件快速定位。

    Args:
        session_id: 会话 ID
        tags: 逗号分隔标签，如 "张三股权转让案,公司法,对赌协议"
        summary: 可选摘要（一两句话概括咨询内容与结论）
    """
    db.init_db()
    conn = db.connect()
    try:
        cur = conn.execute("SELECT session_id FROM sessions WHERE session_id = ?", (session_id,))
        if not cur.fetchone():
            return f"会话 {session_id} 不在库中。先跑 sync_sessions_tool 同步。"
        with db.session_write_lock():
            if summary:
                conn.execute("UPDATE sessions SET tags = ?, summary = ? WHERE session_id = ?",
                             (tags.strip(), summary.strip(), session_id))
            else:
                conn.execute("UPDATE sessions SET tags = ? WHERE session_id = ?",
                             (tags.strip(), session_id))
            conn.commit()
        return f"已归档：{session_id} → 标签[{tags}]"
    finally:
        conn.close()


# ==========================================================================
# 工具 5：导出
# ==========================================================================

@mcp.tool
def export_session(session_id: str, out_dir: str = "") -> str:
    """把会话导出为 Markdown 文件（默认 ~/Desktop/dsh_sessions/），可贴入工作底稿。

    Args:
        session_id: 会话 ID
        out_dir: 可选输出目录（默认桌面 dsh_sessions）
    """
    db.init_db()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return f"会话 {session_id} 不在库中。先跑 sync_sessions_tool 同步。"

    msgs = load_session_messages(session_id, include_tool_calls=True, limit=5000)
    target_dir = out_dir.strip() or DEFAULT_EXPORT_DIR
    os.makedirs(target_dir, exist_ok=True)
    safe_title = "".join(c for c in (row["title"] or session_id) if c not in '\\/:*?"<>|').strip()[:50]
    path = os.path.join(target_dir, f"{safe_title or session_id}.md")

    lines = [
        f"# {row['title']}",
        "",
        f"- 会话 ID: {session_id}",
        f"- 时间: {_fmt_ms(row['created_at'])}",
        f"- 模型: {row['provider']}/{row['model']}" if row["model"] else "- 模型: 未知",
        f"- 标签: {row['tags'] or '无'}",
        f"- 摘要: {row['summary'] or '无'}",
        "",
    ]
    for m in msgs:
        ts = _fmt_ms(m["time"])
        if m["content_type"] == "tool-call":
            lines.append(f"> [{ts}] 工具调用 `{m['tool_name']}`：`{m['content'][:200]}`")
            lines.append("")
        else:
            who = "👤 用户" if m["role"] == "user" else "🤖 助手"
            lines.append(f"## {who} · {ts}")
            lines.append("")
            lines.append(m["content"])
            lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return f"已导出 {len(msgs)} 条消息 → {path}"


# ==========================================================================
# 工具 6：统计
# ==========================================================================

@mcp.tool
def session_stats() -> str:
    """查看会话库概况：总量、时间范围、按模型分布、已归档（有标签）数量。"""
    db.init_db()
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        if not total:
            return "会话库为空。先跑 sync_sessions_tool 同步 DSH 历史会话。"
        rng = conn.execute("SELECT MIN(created_at) AS a, MAX(created_at) AS b FROM sessions").fetchone()
        tagged = conn.execute("SELECT COUNT(*) AS n FROM sessions WHERE tags != ''").fetchone()["n"]
        by_model = conn.execute(
            "SELECT COALESCE(model,'未知') AS m, COUNT(*) AS n FROM sessions GROUP BY model ORDER BY n DESC"
        ).fetchall()
        msg_count = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE content_type='text'").fetchone()["n"]
    finally:
        conn.close()
    lines = [
        f"会话库: {db.DB_PATH}",
        f"会话总数: {total}（已归档打标签 {tagged}），对话消息 {msg_count} 条",
        f"时间范围: {_fmt_ms(rng['a'])} ~ {_fmt_ms(rng['b'])}",
        "按模型分布:",
    ]
    for r in by_model:
        lines.append(f"  - {r['m']}: {r['n']} 个会话")
    return "\n".join(lines)


if __name__ == "__main__":
    logger.info("Starting session-storage MCP server")
    db.init_db()
    logger.info("session-storage MCP ready; transport is managed by the MCP client")
    mcp.run()
