# -*- coding: utf-8 -*-
"""
session-storage.bridge — DSH 插件桥接层
=========================================

供 session-storage-fs DSH 插件通过 child_process 调用，
将 DSH 的会话操作请求转发到 SQLite+FTS5 后端。

用法：
    python bridge.py sync
    python bridge.py search --query "合同法" --limit 10
    python bridge.py get --session-id <id> [--include-tool-calls]
    python bridge.py tag --session-id <id> --tags "标签1,标签2" [--summary "摘要"]
    python bridge.py export --session-id <id> [--out-dir <dir>]
    python bridge.py stats
    python bridge.py append --session-id <id> --type <type> --data <json> --time <ms>
    python bridge.py replay --session-id <id>

所有输出均为 JSON 格式到 stdout。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 确保可以 import 同目录的 db / importer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from importer import load_session_messages, sync_sessions


# ============================================================================
# 子命令实现
# ============================================================================

def cmd_sync(_args):
    """同步 DSH Desktop 会话文件到 SQLite"""
    stats = sync_sessions()
    print(json.dumps(stats, ensure_ascii=False))


def cmd_search(args):
    """全文检索历史会话"""
    db.init_db()
    conn = db.connect()
    try:
        query = args.query
        limit = args.limit
        tag = args.tag or ""

        params = []
        where_extra = ""
        if tag:
            where_extra = " AND s.tags LIKE ?"
            params.append(f"%{tag}%")

        hit_ids = []
        frags = {}

        if len(query.strip()) >= 3:
            phrase = '"' + query.strip().replace('"', '""') + '"'
            fts_rows = conn.execute(
                """SELECT session_id,
                          snippet(messages_fts, 0, '「', '」', '…', 12) AS frag
                   FROM messages_fts WHERE messages_fts MATCH ?
                   ORDER BY rowid LIMIT 200""",
                (phrase,),
            ).fetchall()
            agg = {}
            for r in fts_rows:
                sid = r["session_id"]
                if sid not in agg:
                    agg[sid] = {"hits": 0, "frag": r["frag"]}
                agg[sid]["hits"] += 1
            ranked = sorted(agg.items(), key=lambda kv: -kv[1]["hits"])[:50]
            hit_ids = [sid for sid, _ in ranked]
            frags = {sid: v["frag"] for sid, v in ranked}

        if not hit_ids:
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
            print(json.dumps({"sessions": [], "query": query, "count": 0}, ensure_ascii=False))
            return

        placeholders = ",".join("?" * len(hit_ids))
        rows = conn.execute(
            f"""SELECT s.* FROM sessions s
                WHERE s.session_id IN ({placeholders}){where_extra}
                ORDER BY s.created_at DESC LIMIT ?""",
            (*hit_ids, *params, limit),
        ).fetchall()

        sessions = []
        for r in rows:
            sess = dict(r)
            sess["fragment"] = frags.get(r["session_id"], "")
            sessions.append(sess)

        print(json.dumps({"sessions": sessions, "query": query, "count": len(sessions)}, ensure_ascii=False))
    finally:
        conn.close()


def cmd_get(args):
    """获取会话详情"""
    db.init_db()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (args.session_id,)).fetchone()
    finally:
        conn.close()

    if not row:
        print(json.dumps(None))
        return

    session = dict(row)
    msgs = load_session_messages(
        args.session_id,
        include_tool_calls=args.include_tool_calls,
        limit=args.limit,
    )
    session["messages"] = msgs
    print(json.dumps(session, ensure_ascii=False))


def cmd_tag(args):
    """给会话打标签"""
    db.init_db()
    conn = db.connect()
    try:
        cur = conn.execute("SELECT session_id FROM sessions WHERE session_id = ?", (args.session_id,))
        if not cur.fetchone():
            print(json.dumps({"success": False, "error": "会话不在库中"}))
            return
        with db.session_write_lock():
            if args.summary:
                conn.execute("UPDATE sessions SET tags = ?, summary = ? WHERE session_id = ?",
                             (args.tags.strip(), args.summary.strip(), args.session_id))
            else:
                conn.execute("UPDATE sessions SET tags = ? WHERE session_id = ?",
                             (args.tags.strip(), args.session_id))
            conn.commit()
        print(json.dumps({"success": True, "session_id": args.session_id, "tags": args.tags}))
    finally:
        conn.close()


def cmd_export(args):
    """导出会话为 Markdown"""
    from datetime import datetime

    db.init_db()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (args.session_id,)).fetchone()
    finally:
        conn.close()

    if not row:
        print(json.dumps({"success": False, "error": "会话不在库中"}))
        return

    row_dict = dict(row)
    msgs = load_session_messages(args.session_id, include_tool_calls=True, limit=5000)
    target_dir = args.out_dir or os.path.expanduser("~/Desktop/dsh_sessions")
    os.makedirs(target_dir, exist_ok=True)

    title = row_dict.get("title") or args.session_id
    safe_title = "".join(c for c in title if c not in '\\/:*?"<>|').strip()[:50]
    path = os.path.join(target_dir, f"{safe_title or args.session_id}.md")

    def fmt_ms(ms):
        if not ms:
            return "未知"
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# {row_dict.get('title', '')}",
        "",
        f"- 会话 ID: {args.session_id}",
        f"- 时间: {fmt_ms(row_dict.get('created_at'))}",
        f"- 模型: {row_dict.get('provider', '')}/{row_dict.get('model', '')}" if row_dict.get("model") else "- 模型: 未知",
        f"- 标签: {row_dict.get('tags') or '无'}",
        f"- 摘要: {row_dict.get('summary') or '无'}",
        "",
    ]
    for m in msgs:
        ts = fmt_ms(m["time"])
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

    print(json.dumps({"success": True, "path": path, "messages": len(msgs)}, ensure_ascii=False))


def cmd_stats(_args):
    """获取会话库统计"""
    from datetime import datetime

    db.init_db()
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        if not total:
            print(json.dumps({"total": 0, "message": "会话库为空"}))
            return
        rng = conn.execute("SELECT MIN(created_at) AS a, MAX(created_at) AS b FROM sessions").fetchone()
        tagged = conn.execute("SELECT COUNT(*) AS n FROM sessions WHERE tags != ''").fetchone()["n"]
        by_model = conn.execute(
            "SELECT COALESCE(model,'未知') AS m, COUNT(*) AS n FROM sessions GROUP BY model ORDER BY n DESC"
        ).fetchall()
        msg_count = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE content_type='text'").fetchone()["n"]
    finally:
        conn.close()

    def fmt_ms(ms):
        if not ms:
            return "未知"
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")

    print(json.dumps({
        "total": total,
        "tagged": tagged,
        "message_count": msg_count,
        "time_range": {"from": fmt_ms(rng["a"]), "to": fmt_ms(rng["b"])},
        "by_model": [{"model": r["m"], "count": r["n"]} for r in by_model],
        "db_path": db.DB_PATH,
    }, ensure_ascii=False))


def cmd_append(args):
    """追加一个会话事件（供 DSH StorageBackend.append 调用）"""
    import time as _time

    db.init_db()
    conn = db.connect()
    try:
        # 解析事件数据
        try:
            data = json.loads(args.data) if args.data else {}
        except json.JSONDecodeError:
            data = {"raw": args.data}

        event_type = args.type or "unknown"
        session_id = args.session_id
        event_time = int(args.time) if args.time else int(_time.time() * 1000)

        # 确保会话记录存在
        existing = conn.execute("SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not existing:
            with db.session_write_lock():
                conn.execute(
                    """INSERT INTO sessions (session_id, title, created_at, source, imported_at)
                       VALUES (?, ?, ?, 'dshevent', ?)""",
                    (session_id, f"会话 {session_id[:8]}...", event_time, int(_time.time() * 1000)),
                )
                conn.commit()

        # 根据事件类型写入消息
        if event_type in ("user/message", "assistant/message"):
            content = data.get("text", "") or data.get("content", "")
            role = "user" if event_type.startswith("user") else "assistant"
            turn = data.get("turn", 0)
            step = data.get("step", 0)

            with db.session_write_lock():
                cur = conn.execute(
                    """INSERT INTO messages (session_id, role, turn, step, content_type, content, time)
                       VALUES (?, ?, ?, ?, 'text', ?, ?)""",
                    (session_id, role, turn, step, content, event_time),
                )
                if content.strip():
                    conn.execute(
                        "INSERT INTO messages_fts (content, session_id, message_id) VALUES (?,?,?)",
                        (content, session_id, cur.lastrowid),
                    )
                conn.commit()

        elif event_type == "tool-call":
            tool_name = data.get("name", "")
            arguments = data.get("arguments", "{}")
            turn = data.get("turn", 0)
            step = data.get("step", 0)

            with db.session_write_lock():
                conn.execute(
                    """INSERT INTO messages (session_id, role, turn, step, content_type, content, tool_name, time)
                       VALUES (?, 'assistant', ?, ?, 'tool-call', ?, ?, ?)""",
                    (session_id, turn, step, arguments, tool_name, event_time),
                )
                conn.commit()

        print(json.dumps({"success": True, "session_id": session_id, "type": event_type}))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
    finally:
        conn.close()


def cmd_replay(args):
    """回放会话全部事件（供 DSH StorageBackend.replay 调用）"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT seq, role, turn, step, content_type, content, tool_name, time
               FROM messages WHERE session_id = ? ORDER BY seq""",
            (args.session_id,),
        ).fetchall()
        messages = [dict(r) for r in rows]
    finally:
        conn.close()

    print(json.dumps({"session_id": args.session_id, "messages": messages, "count": len(messages)}, ensure_ascii=False))


# ============================================================================
# CLI 入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="session-storage DSH 桥接层")
    sub = parser.add_subparsers(dest="command")

    # sync
    sub.add_parser("sync", help="同步 DSH 会话文件")

    # search
    p_search = sub.add_parser("search", help="全文检索会话")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--tag", default="")

    # get
    p_get = sub.add_parser("get", help="获取会话详情")
    p_get.add_argument("--session-id", required=True)
    p_get.add_argument("--include-tool-calls", action="store_true")
    p_get.add_argument("--limit", type=int, default=200)

    # tag
    p_tag = sub.add_parser("tag", help="给会话打标签")
    p_tag.add_argument("--session-id", required=True)
    p_tag.add_argument("--tags", required=True)
    p_tag.add_argument("--summary", default="")

    # export
    p_export = sub.add_parser("export", help="导出会话为 Markdown")
    p_export.add_argument("--session-id", required=True)
    p_export.add_argument("--out-dir", default="")

    # stats
    sub.add_parser("stats", help="会话库统计")

    # append
    p_append = sub.add_parser("append", help="追加会话事件")
    p_append.add_argument("--session-id", required=True)
    p_append.add_argument("--type", required=True)
    p_append.add_argument("--data", default="{}")
    p_append.add_argument("--time", default="")

    # replay
    p_replay = sub.add_parser("replay", help="回放会话事件")
    p_replay.add_argument("--session-id", required=True)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmd_map = {
        "sync": cmd_sync,
        "search": cmd_search,
        "get": cmd_get,
        "tag": cmd_tag,
        "export": cmd_export,
        "stats": cmd_stats,
        "append": cmd_append,
        "replay": cmd_replay,
    }

    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
