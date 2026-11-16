#!/usr/bin/env python3
"""思考开/关对照：同一判决、同一问题，比较事实准确率、耗时与思维链开销。

模式：
  off = /no_think + enable_thinking=false，reasoning_tokens 应为 0
  on  = enable_thinking=true + reasoning_effort=medium

运行：
    /Users/simon/Documents/1-SLP/venv/bin/python tests/ab_thinking_benchmark.py

结果：
    tests/results/ab_thinking_<时间>.json
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PDF = (
    "/Users/simon/Documents/2-WorkSpace/2-DeepSeek Harness/4-案件分析梳理/"
    "（2026）京03民终13295号债权人撤销权判决书.pdf"
)
MODEL = "qwen/qwen3.8-27b"
API_URL = "http://127.0.0.1:1234/v1"

SYSTEM = (
    "你是中国大陆执业律师。仅依据给定的二审民事判决书回答，不引用卷宗之外的事实。"
    "先给结论，再给依据；案号、金额、日期必须与原文一致。"
)

QUESTIONS: List[Dict[str, Any]] = [
    {
        "id": "Q1",
        "question": (
            "请提取本案的基本信息：二审案号、二审法院、上诉人、被上诉人、"
            "一审法院及一审案号。分条列出，不要分析。"
        ),
        "limit": 300,
        "facts": [
            ("二审案号", [r"2026.{0,2}京03民终13295号"]),
            ("二审法院", [r"北京市第三中级人民法院"]),
            ("上诉人", [r"郝建平", r"熊学萍", r"郝雪"], "all"),
            ("被上诉人", [r"马赶平"]),
            ("一审案号", [r"2025.{0,2}京0105民初90109号"]),
            ("一审法院", [r"北京市朝阳区人民法院"]),
        ],
    },
    {
        "id": "Q2",
        "question": (
            "请回答：马赶平对建业公司的债权本金是多少、债权形成于何时；"
            "郝建平、熊学萍抽逃出资多少、发生于何时；追加被执行人的裁定案号、"
            "生效时间及责任范围。分条列出。"
        ),
        "limit": 400,
        "facts": [
            ("债权本金", [r"8[，,]?735[，,]?573元", r"8735573"]),
            ("债权形成时间", [r"2016年11月"]),
            ("抽逃出资额", [r"1250万元", r"1[，,]?250万元", r"12[，,]?500[，,]?000元"]),
            ("追加裁定案号", [r"冀0982执异100-1号"]),
            ("追加裁定生效时间", [r"2024年9月23日"]),
            ("责任范围", [r"抽逃出资.{0,12}范围内"]),
        ],
    },
    {
        "id": "Q3",
        "question": (
            "请回答：案涉房屋何时转让、转让人与受让人分别是谁；合同与发票载明的"
            "成交价、上诉人主张的总价、装修补偿款、抵押评估价值分别是多少；"
            "法院最终认定的房屋价格是多少。分条列出。"
        ),
        "limit": 400,
        "facts": [
            ("转让时间", [r"2020年7月24日"]),
            ("转让人", [r"熊学萍"]),
            ("受让人", [r"郝雪"]),
            ("合同价", [r"468万元"]),
            ("主张总价", [r"890万元"]),
            ("装修补偿", [r"422万元"]),
            ("抵押评估", [r"1000万元"]),
        ],
    },
    {
        "id": "Q4",
        "question": (
            "请归纳本案二审的两个争议焦点，并说明法院为什么认定郝建平、熊学萍"
            "在房屋转让时已经是马赶平的债务人。要求写出时间链条和裁判逻辑，"
            "不要只给结论。"
        ),
        "limit": 700,
        "facts": [
            ("焦点一", [r"是否.{0,30}债权"]),
            ("焦点二", [r"(交易|转让).{0,25}(影响|损害).{0,12}债权"]),
            ("追加裁定性质", [r"(不改变|并非|不是).{0,25}(形成时间|事实基础)"]),
            ("责任根源", [r"抽逃出资.{0,50}2013"]),
            ("债权形成", [r"2016年11月"]),
            ("转让时间", [r"2020年7月24日"]),
            ("上诉不成立", [r"(上诉请求|上诉主张).{0,12}(不能成立|不成立)"]),
        ],
    },
    {
        "id": "Q5",
        "question": (
            "请回答：本案一年除斥期间的起算与起诉时间、是否超过；"
            "二审判决结果和二审案件受理费是多少。分条列出。"
        ),
        "limit": 350,
        "facts": [
            ("一年除斥期间", [r"一年.{0,12}除斥期间", r"除斥期间.{0,12}一年"]),
            ("查询时间", [r"2025年4月17日"]),
            ("起诉时间", [r"2025年6月6日"]),
            ("未超过一年", [r"(未|没有).{0,12}超过.{0,8}一年"]),
            ("判决结果", [r"驳回上诉.{0,12}维持原判"]),
            ("二审受理费", [r"44[，,]?240元"]),
        ],
    },
]

# 判决书中出现过的案号（含一审、执行异议、关联判决）。
ALLOWED_CASE_TOKENS = (
    "13295", "90109", "100-1", "1001", "1876", "6913", "2761",
)
CASE_RE = re.compile(r"（?(?:19|20)\d{2}）?[^\s，。；、,;]{0,28}?号")


def normalize(text: str) -> str:
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("，", ",").replace("：", ":")
    return re.sub(r"\s+", "", text)


def call_model(
    client: OpenAI,
    prompt: str,
    *,
    mode: str,
    max_tokens: int,
) -> Dict[str, Any]:
    user = prompt + ("\n/no_think" if mode == "off" else "")
    extra: Dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": mode == "on"}}
    if mode == "on":
        extra["reasoning_effort"] = "medium"

    started = time.perf_counter()
    first_token: Optional[float] = None
    answer_start: Optional[float] = None
    reasoning = ""
    content = ""
    usage = None
    finish: Optional[str] = None
    error: Optional[str] = None

    try:
        stream = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=extra,
        )
        for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta
                piece_r = getattr(delta, "reasoning_content", None) or (
                    (getattr(delta, "model_extra", None) or {}).get("reasoning_content")
                )
                piece_c = getattr(delta, "content", None)
                if piece_r or piece_c:
                    if first_token is None:
                        first_token = time.perf_counter() - started
                if piece_r:
                    reasoning += piece_r
                if piece_c:
                    if answer_start is None:
                        answer_start = time.perf_counter() - started
                    content += piece_c
                if chunk.choices[0].finish_reason:
                    finish = chunk.choices[0].finish_reason
            if getattr(chunk, "usage", None):
                usage = chunk.usage
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:400]}"

    details = getattr(usage, "completion_tokens_details", None) if usage else None
    return {
        "ok": error is None,
        "error": error,
        "answer": content,
        "reasoning": reasoning,
        "first_token_s": round(first_token, 2) if first_token else None,
        "answer_start_s": round(answer_start, 2) if answer_start else None,
        "total_s": round(time.perf_counter() - started, 2),
        "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        "reasoning_tokens": getattr(details, "reasoning_tokens", None) if details else None,
        "finish_reason": finish,
    }


def score(answer: str, facts: List[Tuple]) -> Dict[str, Any]:
    text = normalize(answer)
    hits: List[str] = []
    misses: List[str] = []

    for item in facts:
        if len(item) == 3 and item[2] == "all":
            name, patterns = item[0], item[1]
            hit = all(re.search(p, text) for p in patterns)
        else:
            name, patterns = item[0], item[1]
            hit = any(re.search(p, text) for p in patterns)
        (hits if hit else misses).append(name)

    cases = [normalize(m.group(0)) for m in CASE_RE.finditer(text)]
    hallucinations = [
        c for c in cases
        if not any(token in c for token in ALLOWED_CASE_TOKENS)
    ]

    return {
        "score": round(len(hits) / max(1, len(facts)), 3),
        "hits": hits,
        "misses": misses,
        "answer_chars": len(answer),
        "case_numbers": cases,
        "hallucinated_cases": hallucinations,
    }


def model_context() -> Tuple[str, int]:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:1234/api/v0/models", timeout=5
        ) as resp:
            for item in json.load(resp).get("data", []):
                if item.get("id") == MODEL:
                    return item.get("state", "unknown"), int(
                        item.get("loaded_context_length") or 0
                    )
    except Exception:
        pass
    return "unavailable", 0


def main() -> int:
    parser = argparse.ArgumentParser(description="思考开/关对照测试")
    parser.add_argument("--pdf", default=DEFAULT_PDF)
    parser.add_argument("--off-max-tokens", type=int, default=2048)
    parser.add_argument("--on-max-tokens", type=int, default=6144)
    parser.add_argument(
        "--out",
        default=str(
            BASE_DIR / "tests" / "results"
            / f"ab_thinking_{datetime.now():%Y%m%d_%H%M}.json"
        ),
    )
    args = parser.parse_args()

    state, ctx = model_context()
    if state != "loaded" or ctx < 65536:
        print(f"模型未就绪：state={state} ctx={ctx}", flush=True)
        return 1

    doc = "\n".join(page.get_text() for page in fitz.open(args.pdf)).strip()
    client = OpenAI(base_url=API_URL, api_key="lm-studio", timeout=900)
    rows: List[Dict[str, Any]] = []

    for q in QUESTIONS:
        prompt = (
            f"【判决书】\n{doc}\n\n【问题】\n{q['question']}\n"
            f"回答控制在 {q['limit']} 字以内。"
        )
        for mode in ("off", "on"):
            max_tokens = (
                args.off_max_tokens if mode == "off" else args.on_max_tokens
            )
            result = call_model(client, prompt, mode=mode, max_tokens=max_tokens)
            result.update(
                {
                    "qid": q["id"],
                    "mode": mode,
                    "mode_config": (
                        "/no_think + enable_thinking=false"
                        if mode == "off"
                        else "enable_thinking=true + reasoning_effort=medium"
                    ),
                    "max_tokens": max_tokens,
                }
            )
            if result["ok"]:
                result.update(score(result["answer"], q["facts"]))
            else:
                result.update(
                    {"score": 0.0, "hits": [], "misses": ["调用失败"],
                     "answer_chars": 0, "case_numbers": [],
                     "hallucinated_cases": []}
                )
            rows.append(result)
            print(
                f"{q['id']} {mode:<3} score={result['score']:.2f} "
                f"total={result['total_s']}s first={result['first_token_s']}s "
                f"answer_at={result['answer_start_s']}s "
                f"reason={result['reasoning_tokens']} "
                f"completion={result['completion_tokens']} "
                f"finish={result['finish_reason']} "
                f"chars={result['answer_chars']} "
                f"miss={result['misses']} "
                f"fake_cases={result['hallucinated_cases']}",
                flush=True,
            )

    summary: Dict[str, Any] = {}
    for mode in ("off", "on"):
        subset = [r for r in rows if r["mode"] == mode]
        summary[mode] = {
            "avg_score": round(
                sum(r["score"] for r in subset) / max(1, len(subset)), 3
            ),
            "avg_total_s": round(
                sum(r["total_s"] for r in subset) / max(1, len(subset)), 2
            ),
            "avg_answer_start_s": round(
                sum(r["answer_start_s"] or 0 for r in subset) / max(1, len(subset)), 2
            ),
            "total_reasoning_tokens": sum(r["reasoning_tokens"] or 0 for r in subset),
            "truncated": sum(
                1 for r in subset if r["finish_reason"] == "length"
            ),
            "empty_answers": sum(1 for r in subset if not r["answer"].strip()),
            "hallucinated_case_count": sum(
                len(r["hallucinated_cases"]) for r in subset
            ),
        }

    per_question = []
    for q in QUESTIONS:
        off = next(r for r in rows if r["qid"] == q["id"] and r["mode"] == "off")
        on = next(r for r in rows if r["qid"] == q["id"] and r["mode"] == "on")
        per_question.append(
            {
                "qid": q["id"],
                "score_off": off["score"],
                "score_on": on["score"],
                "total_off_s": off["total_s"],
                "total_on_s": on["total_s"],
                "reasoning_tokens_on": on["reasoning_tokens"],
                "miss_off": off["misses"],
                "miss_on": on["misses"],
            }
        )

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        "loaded_context": ctx,
        "pdf": args.pdf,
        "doc_chars": len(doc),
        "system": SYSTEM,
        "questions": [{k: q[k] for k in ("id", "question", "limit")} for q in QUESTIONS],
        "results": rows,
        "summary": summary,
        "per_question": per_question,
        "thinking_off_verified": all(
            (r["reasoning_tokens"] or 0) == 0 for r in rows if r["mode"] == "off"
        ),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print("\n== summary ==", flush=True)
    for mode in ("off", "on"):
        print(mode, json.dumps(summary[mode], ensure_ascii=False), flush=True)
    print("\n== per question ==", flush=True)
    for item in per_question:
        print(json.dumps(item, ensure_ascii=False), flush=True)
    print(f"\nreport: {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
