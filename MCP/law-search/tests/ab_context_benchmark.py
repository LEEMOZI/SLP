#!/usr/bin/env python3
"""P1：真实 64K vs 128K 本地上下文 A/B 基准。

方法：
  1. 用 36 页真实判决 PDF 文本构造四档 prompt（自然长度 / ~32K / ~58K / >64K）；
  2. 分别在 qwen3.8-27b 以 64K、128K 加载时发送同一组请求；
  3. 记录首 token 延迟（TTFT）、总耗时、实际 prompt tokens、生成 tokens、成功率；
  4. 结束后恢复 128K + 24h TTL 常驻。

运行：
    /Users/simon/Documents/1-SLP/venv/bin/python tests/ab_context_benchmark.py

注意：测试期间会卸载并重载 LM Studio 模型，约 10–15 分钟，期间本地推理不可用。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from config import LEGAL_SYSTEM_PROMPT  # noqa: E402

LMS = Path.home() / ".cache" / "lm-studio" / "bin" / "lms"
MODEL = "qwen/qwen3.8-27b"
BASE_URL = "http://127.0.0.1:1234"
API_URL = f"{BASE_URL}/v1"
DEFAULT_PDF = (
    "/Users/simon/Documents/2-WorkSpace/2-DeepSeek Harness/4-案件分析梳理/"
    "（2026）京03民终13295号债权人撤销权判决书.pdf"
)
CONTEXTS = (65536, 131072)

INSTRUCTION = (
    "请阅读以下卷宗材料，完成：\n"
    "1. 提取本案案号、当事人、争议焦点；\n"
    "2. 概括裁判结果与核心理由；\n"
    "3. 用 200 字总结裁判要旨。\n\n"
    "【卷宗开始】\n{body}\n【卷宗结束】"
)


def _lms(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(LMS), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _model_status(model: str) -> tuple[str, int]:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/v0/models", timeout=5) as resp:
            data = json.load(resp).get("data", [])
    except Exception:
        return "unavailable", 0
    for item in data:
        if item.get("id") == model:
            return item.get("state", "unknown"), int(
                item.get("loaded_context_length") or 0
            )
    return "missing", 0


def _wait_loaded(model: str, context: int, timeout: int = 420) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state, ctx = _model_status(model)
        if state == "loaded" and ctx == context:
            return
        time.sleep(3)
    state, ctx = _model_status(model)
    raise RuntimeError(f"model not ready: state={state} ctx={ctx}")


def _estimate_memory(context: int) -> Optional[float]:
    out = _lms(
        "load", MODEL, "-c", str(context), "--gpu", "max",
        "--parallel", "4", "--estimate-only", "-y",
    ).stdout
    for line in out.splitlines():
        if "Estimated Total Memory" in line:
            try:
                return float(line.split(":")[1].strip().split()[0])
            except (IndexError, ValueError):
                return None
    return None


def _reload(context: int, ttl: int = 3600) -> None:
    _lms("unload", MODEL)
    _lms(
        "load", MODEL, "-c", str(context), "--gpu", "max",
        "--parallel", "4", "--ttl", str(ttl), "-y",
    )
    _wait_loaded(MODEL, context)


def _delta_text(delta: Any) -> str:
    """Qwen3.8 默认输出思维链，流式增量可能在 reasoning_content 里。"""
    for field in ("content", "reasoning_content", "reasoning"):
        value = getattr(delta, field, None)
        if value:
            return value
    extra = getattr(delta, "model_extra", None) or {}
    for field in ("reasoning_content", "reasoning"):
        value = extra.get(field)
        if value:
            return value
    return ""


def measure(
    client: OpenAI,
    messages: List[Dict[str, str]],
    *,
    max_tokens: int,
) -> Dict[str, Any]:
    started = time.perf_counter()
    ttft: Optional[float] = None
    usage = None
    error: Optional[str] = None
    chars = 0

    try:
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0,
            stream=True,
            stream_options={"include_usage": True},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        for chunk in stream:
            if chunk.choices:
                piece = _delta_text(chunk.choices[0].delta)
                if piece:
                    chars += len(piece)
                    if ttft is None:
                        ttft = time.perf_counter() - started
            if getattr(chunk, "usage", None):
                usage = chunk.usage
    except Exception as exc:  # noqa: BLE001 — 失败本身就是要观测的结果
        error = f"{type(exc).__name__}: {str(exc)[:400]}"

    return {
        "ok": error is None,
        "error": error,
        "ttft_s": round(ttft, 3) if ttft is not None else None,
        "total_s": round(time.perf_counter() - started, 2),
        "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "completion_tokens": (
            getattr(usage, "completion_tokens", None) if usage else None
        ),
        "reasoning_tokens": (
            getattr(usage.completion_tokens_details, "reasoning_tokens", None)
            if usage and getattr(usage, "completion_tokens_details", None)
            else None
        ),
        "output_chars": chars,
    }


def make_cases(doc: str, natural_tokens: int) -> List[Dict[str, Any]]:
    def copies_for(target: int) -> int:
        return max(1, int(target // max(1, natural_tokens)))

    return [
        {"name": "natural", "copies": 1, "target": "自然长度"},
        {"name": "mid_32k", "copies": copies_for(32_000), "target": "~32K"},
        {"name": "near_64k", "copies": copies_for(58_000), "target": "~58K"},
        {"name": "over_64k", "copies": copies_for(80_000), "target": "~80K"},
    ]


def run_phase(
    doc: str,
    cases: List[Dict[str, Any]],
    context: int,
    *,
    max_tokens: int,
) -> List[Dict[str, Any]]:
    _reload(context)
    client = OpenAI(base_url=API_URL, api_key="lm-studio", timeout=900)
    rows: List[Dict[str, Any]] = []

    for case in cases:
        body = ("\n\n----- 卷宗分册 -----\n\n").join([doc] * case["copies"])
        messages = [
            {"role": "system", "content": LEGAL_SYSTEM_PROMPT},
            {"role": "user", "content": INSTRUCTION.format(body=body)},
        ]
        result = measure(client, messages, max_tokens=max_tokens)
        result.update(
            {"case": case["name"], "copies": case["copies"], "context": context}
        )
        rows.append(result)
        print(
            f"  ctx={context:>6} case={case['name']:<9} copies={case['copies']} "
            f"ok={result['ok']} prompt_tokens={result['prompt_tokens']} "
            f"ttft={result['ttft_s']}s total={result['total_s']}s "
            f"error={result['error']}"
        )
    return rows


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_ctx: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        by_ctx.setdefault(row["context"], {})[row["case"]] = row

    comparisons = []
    for case in by_ctx.get(131072, {}):
        a = by_ctx.get(65536, {}).get(case)
        b = by_ctx[131072].get(case)
        if not a or not b:
            continue
        comparisons.append(
            {
                "case": case,
                "ok_64k": a["ok"],
                "ok_128k": b["ok"],
                "prompt_tokens_64k": a["prompt_tokens"],
                "prompt_tokens_128k": b["prompt_tokens"],
                "ttft_64k_s": a["ttft_s"],
                "ttft_128k_s": b["ttft_s"],
                "total_64k_s": a["total_s"],
                "total_128k_s": b["total_s"],
                "total_delta_s": (
                    round(b["total_s"] - a["total_s"], 2) if a["ok"] and b["ok"] else None
                ),
                "total_delta_pct": (
                    round((b["total_s"] - a["total_s"]) / a["total_s"] * 100, 1)
                    if a["ok"] and b["ok"] and a["total_s"]
                    else None
                ),
            }
        )
    return {"comparisons": comparisons}


def main() -> int:
    parser = argparse.ArgumentParser(description="64K vs 128K 上下文 A/B 基准")
    parser.add_argument("--pdf", default=DEFAULT_PDF)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument(
        "--out",
        default=str(
            BASE_DIR
            / "tests"
            / "results"
            / f"ab_context_{datetime.now():%Y%m%d_%H%M}.json"
        ),
    )
    args = parser.parse_args()

    doc = "\n".join(page.get_text() for page in fitz.open(args.pdf))
    doc = doc.strip()
    print(f"PDF: {args.pdf}")
    print(f"chars={len(doc):,}")

    memory = {str(ctx): _estimate_memory(ctx) for ctx in CONTEXTS}

    # 校准：当前应为 128K 常驻，用自然长度 prompt 取得真实 token 数
    state, ctx = _model_status(MODEL)
    if state != "loaded":
        _reload(131072)
    client = OpenAI(base_url=API_URL, api_key="lm-studio", timeout=900)
    natural = measure(
        client,
        [
            {"role": "system", "content": LEGAL_SYSTEM_PROMPT},
            {"role": "user", "content": INSTRUCTION.format(body=doc)},
        ],
        max_tokens=32,
    )
    natural_tokens = natural["prompt_tokens"] or 0
    print(f"calibration: natural prompt_tokens={natural_tokens}")

    if natural_tokens <= 0:
        print("校准失败，无法确定 prompt token 规模", file=sys.stderr)
        return 1

    cases = make_cases(doc, natural_tokens)
    print(
        "cases:",
        ", ".join(f"{c['name']}x{c['copies']}" for c in cases),
    )

    rows: List[Dict[str, Any]] = []
    restored = False
    try:
        for context in CONTEXTS:
            print(f"== phase {context} ==")
            rows.extend(
                run_phase(doc, cases, context, max_tokens=args.max_tokens)
            )
    finally:
        print("restoring 128K / 24h TTL ...")
        _reload(131072, ttl=86400)
        restored = True

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "machine": "Mac Studio M5 Max / 64GB",
        "model": MODEL,
        "pdf": args.pdf,
        "doc_chars": len(doc),
        "max_tokens": args.max_tokens,
        "memory_estimate_gib": memory,
        "calibration_prompt_tokens": natural_tokens,
        "cases": cases,
        "results": rows,
        "summary": summarize(rows),
        "restored_128k": restored,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print("\n== summary ==")
    for item in report["summary"]["comparisons"]:
        print(
            f"{item['case']:<9} prompt={item['prompt_tokens_64k']} "
            f"ok64={item['ok_64k']} ok128={item['ok_128k']} "
            f"ttft64={item['ttft_64k_s']}s ttft128={item['ttft_128k_s']}s "
            f"total64={item['total_64k_s']}s total128={item['total_128k_s']}s "
            f"delta={item['total_delta_pct']}%"
        )
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
