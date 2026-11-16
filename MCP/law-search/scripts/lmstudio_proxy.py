#!/usr/bin/env python3
"""LM Studio 自动换模代理。

背景：64 GB 统一内存下 qwen/qwen3.6-35b-a3b（29.4 GB）与 qwen/qwen3.8-27b
（128K 需 42.2 GB）无法同时驻留。LM Studio 的内存护栏不会自动换出已驻留
模型，而是直接返回 HTTP 400：

    Failed to load model "qwen/qwen3.8-27b" ...
    Model loading was stopped due to insufficient system resources.

客户端（Cherry / DSH / WorkBuddy / Bionic）选模型时发的是直连
/v1/chat/completions，不经过 SLP，所以 SLP 内部的 auto_swap 覆盖不到。

本代理监听 1235，转发到 LM Studio 1234；转发前若目标模型与当前驻留的
LLM 不同，先 unload 再 load（128K），随后原样流式转发响应。
客户端只需把 base URL 从 :1234 改成 :1235。

限制：换模会打断正在进行的另一个模型的流式请求；同一模型连续请求不受影响。
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.lmstudio_control import ensure_model_loaded  # noqa: E402

UPSTREAM_HOST = os.environ.get("LMSTUDIO_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("LMSTUDIO_UPSTREAM_PORT", "1234"))
LISTEN_HOST = os.environ.get("LMSTUDIO_PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LMSTUDIO_PROXY_PORT", "1235"))
DEFAULT_CONTEXT = int(os.environ.get("LMSTUDIO_PROXY_CONTEXT", "131072"))
UPSTREAM_TIMEOUT = int(os.environ.get("LMSTUDIO_PROXY_TIMEOUT", "900"))

# 嵌入模型与主 LLM 互不影响，不做换模
NO_SWAP_PATHS = ("/v1/embeddings", "/api/v0/models/load", "/api/v0/models/unload")

_SWAP_LOCK = threading.Lock()

_HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}


def log(message: str) -> None:
    print(f"[lmstudio-proxy] {message}", flush=True)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lmstudio-swap-proxy/1.0"

    # ---- 日志：减少 BaseHTTPRequestHandler 的默认噪音 ----
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log("%s %s" % (self.address_string(), fmt % args))

    # ---- 所有方法统一走转发 ----
    def do_GET(self) -> None:
        self._relay("GET")

    def do_HEAD(self) -> None:
        self._relay("HEAD")

    def do_POST(self) -> None:
        self._relay("POST")

    def do_DELETE(self) -> None:
        self._relay("DELETE")

    # ---- 内部 ----
    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _maybe_swap(self, body: bytes) -> None:
        if not body or self.path.startswith(NO_SWAP_PATHS):
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        model = payload.get("model") if isinstance(payload, dict) else None
        if not model:
            return

        ctx = payload.get("context_length") or DEFAULT_CONTEXT
        try:
            ctx = int(ctx)
        except (TypeError, ValueError):
            ctx = DEFAULT_CONTEXT

        # 换模本身会写较多日志，且可能耗时 ~10s
        with _SWAP_LOCK:
            try:
                if ensure_model_loaded(model, ctx):
                    log(f"swapped to {model} @ {ctx} ctx")
            except Exception as exc:  # 换模失败交给上游报真实错误
                log(f"swap to {model} failed: {exc}")

    def _relay(self, method: str) -> None:
        body = self._read_body() if method in ("POST", "PUT", "PATCH") else b""

        if method == "POST":
            self._maybe_swap(body)

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP
        }

        upstream = http.client.HTTPConnection(
            UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT
        )
        try:
            upstream.request(
                method, self.path, body=body or None, headers=headers
            )
            resp = upstream.getresponse()

            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected during stream")
        except Exception as exc:
            log(f"upstream error: {type(exc).__name__}: {exc}")
            try:
                self.send_error(502, "upstream unavailable")
            except Exception:
                pass
        finally:
            upstream.close()


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.daemon_threads = True
    log(
        f"listening {LISTEN_HOST}:{LISTEN_PORT} -> "
        f"{UPSTREAM_HOST}:{UPSTREAM_PORT} | default_ctx={DEFAULT_CONTEXT}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
