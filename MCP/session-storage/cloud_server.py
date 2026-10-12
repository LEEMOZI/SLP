# -*- coding: utf-8 -*-
"""
session-storage-cloud MCP Server
================================

session-storage 的 streamable-http 远程版本（端口 8767）。

与本地 stdio 版共享同一个 data/sessions.db 数据库——两台 DSH
（MacStudio + MacBook + 未来更多设备）通过 Bearer Token 鉴权访问，
共享同一份会话记忆库。

同步策略：
  - sync_sessions 仍从 Studio 本地 DSH 会话目录增量导入
  - 新增 push_session 工具：远端设备主动推送会话 JSON，写入共享库
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("session-storage.cloud")

# 确保可以 import 同目录的 db / server / importer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from server import mcp  # 复用 server.py 的 6 个工具 + FastMCP 对象

# --------------------------------------------------------------------------
# 新增工具 7：远端推送会话
# --------------------------------------------------------------------------

@mcp.tool
def push_session(
    session_id: str,
    title: str = "",
    provider: str = "",
    model: str = "",
    messages_json: str = "[]",
    created_at_ms: int = 0,
    source: str = "remote",
) -> str:
    """把远端 DSH 的一个会话推送入库（跨设备共享记忆的核心通道）。

    何时调用：
    - MacBook 或未来更多设备的 DSH 会话结束后，主动推送到 Studio 共享库
    - 直接调用会自动 upsert（已存在则覆盖），幂等安全

    Args:
        session_id: 会话 ID（如 session-abc123... 或 UUID）
        title: 会话标题
        provider: 模型提供商（如 deepseek）
        model: 模型名（如 deepseek-v3）
        messages_json: 消息数组 JSON，格式：
            [{"role":"user","content":"你好","time":1712345678000},
             {"role":"assistant","content":"你好！","time":1712345678100}]
            或带 tool-call：
            [{"role":"assistant","content":"{}","tool_name":"search_law",
              "content_type":"tool-call","time":1712345678200}]
        created_at_ms: 会话创建时间（ms epoch，0=自动取首条消息时间）
        source: 来源标记（默认 remote，区分 import / dshevent / manual）
    """
    result = _do_bulk_push({
        "session_id": session_id,
        "title": title,
        "provider": provider,
        "model": model,
        "messages_json": messages_json,
        "created_at_ms": created_at_ms,
        "source": source,
    })
    if not result.get("success"):
        return f"推送失败：{result.get('error')}"
    fmt = datetime.fromtimestamp(
        created_at_ms / 1000 if created_at_ms else time.time()
    ).strftime("%Y-%m-%d %H:%M")
    return (
        f"已推送 {result['messages']} 条消息 → {session_id}\n"
        f"标题: {title or '(无)'}\n"
        f"时间: {fmt} | 来源: {source}"
    )


# --------------------------------------------------------------------------
# HTTP 传输层（复用 law-search-cloud 模式）
# --------------------------------------------------------------------------

def _do_bulk_push(payload: dict) -> dict:
    """纯函数：把 push_session 的核心逻辑抽出来，MCP 工具和 /bulk-push 端点共用。"""
    db.init_db()
    conn = db.connect()
    try:
        session_id = payload.get("session_id")
        messages_json = payload.get("messages_json") or payload.get("messages")
        title = payload.get("title", "")
        provider = payload.get("provider", "")
        model = payload.get("model", "")
        created_at_ms = int(payload.get("created_at_ms") or 0)
        source = payload.get("source", "remote")

        if not session_id:
            return {"success": False, "error": "缺少 session_id"}

        if isinstance(messages_json, str):
            try:
                messages = json.loads(messages_json)
            except Exception:
                return {"success": False, "error": "messages_json 解析失败"}
        elif isinstance(messages_json, list):
            messages = messages_json
        else:
            return {"success": False, "error": "缺少 messages"}

        if not created_at_ms and messages:
            first_time = messages[0].get("time")
            if first_time:
                created_at_ms = int(first_time)
        created_at_ms = created_at_ms or int(time.time() * 1000)

        with db.session_write_lock():
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
            conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (session_id, title, created_at, provider, model, source, imported_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (session_id, title or f"会话 {session_id[:8]}...",
                 created_at_ms, provider, model, source, int(time.time() * 1000)),
            )
            seq = 0
            for m in messages:
                role = m.get("role", "user")
                content_type = m.get("content_type", "text")
                content = m.get("content", "")
                tool_name = m.get("tool_name") if content_type == "tool-call" else None
                msg_time = int(m.get("time") or created_at_ms)
                conn.execute(
                    """INSERT INTO messages
                       (session_id, seq, role, turn, step, content_type,
                        content, tool_name, time)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (session_id, seq, role, 0, 0, content_type, content, tool_name, msg_time),
                )
                if content_type == "text" and content.strip():
                    conn.execute(
                        "INSERT INTO messages_fts (content, session_id, message_id) VALUES (?,?,?)",
                        (content, session_id, seq),
                    )
                seq += 1
            conn.execute("UPDATE sessions SET turns = ? WHERE session_id = ?", (seq, session_id))
            conn.commit()

        return {"success": True, "session_id": session_id, "messages": seq}
    except Exception as exc:
        conn.rollback()
        return {"success": False, "error": str(exc)}
    finally:
        conn.close()


def _build_wrapped_app(asgi_app, token: str, lifespan):
    """裸 ASGI app → Starlette + Bearer Token 鉴权中间件（与 law-search-cloud 同款）。"""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount

    _token = token

    async def _health(_request):
        db.init_db()
        conn = db.connect()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        finally:
            conn.close()
        return JSONResponse({
            "service": "session-storage",
            "transport": "streamable-http",
            "sessions": total,
            "db_path": db.DB_PATH,
            "ok": True,
        })

    async def _root(_request):
        msg = (
            "session-storage-cloud MCP\n"
            "- transport:  streamable-http\n"
            "- endpoint:   /mcp (MCP protocol) | /bulk-push (JSON POST, 插件用)\n"
            "- auth:       Bearer Token required\n"
            f"- sessions:   (see /health)\n"
        )
        return PlainTextResponse(msg)

    async def _bulk_push(request):
        """插件专用端点：一次性推送整会话（绕开 MCP 协议，纯 JSON POST）。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "JSON 解析失败"}, status_code=400)
        result = _do_bulk_push(body)
        return JSONResponse(result)

    PUBLIC_PATHS = frozenset({"/health", "/healthz", "/", "/favicon.ico"})

    class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if path in PUBLIC_PATHS:
                return await call_next(request)
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse(
                    {"error": "Missing or invalid Authorization header"}, status_code=401,
                )
            provided = auth_header[7:]
            if not secrets.compare_digest(provided, _token):
                return JSONResponse(
                    {"error": "Invalid bearer token"}, status_code=401,
                )
            return await call_next(request)

    wrapped = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/", endpoint=_root),
            Route("/health", endpoint=_health),
            Route("/healthz", endpoint=_health),
            Route("/bulk-push", endpoint=_bulk_push, methods=["POST"]),
            Mount("/", app=asgi_app),
        ],
    )
    wrapped.add_middleware(BearerTokenAuthMiddleware)
    return wrapped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="session-storage MCP cloud server")
    parser.add_argument(
        "--transport", choices=["stdio", "http", "sse"], default="stdio",
        help="MCP transport（默认 stdio；http=streamable HTTP 远程接入）",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--auth-token", default="abcd1234ef",
        help="Bearer Token（HTTP/SSE 下生效；默认 abcd1234ef）",
    )
    args = parser.parse_args()

    token = args.auth_token if args.transport in ("http", "sse") else ""

    db.init_db()

    if args.transport == "http":
        import uvicorn
        mcp_app = mcp.http_app(transport="http")
        asgi_app = _build_wrapped_app(mcp_app, token, lifespan=mcp_app.lifespan)
        logger.info(
            "session-storage-cloud | streamable-http + Bearer Token | %s:%s/mcp | DB=%s",
            args.host, args.port, db.DB_PATH,
        )
        uvicorn.run(asgi_app, host=args.host, port=args.port)
    else:
        logger.info("session-storage MCP ready; transport=stdio")
        mcp.run()
