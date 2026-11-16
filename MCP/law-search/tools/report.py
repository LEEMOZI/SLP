#!/usr/bin/env python3
"""P1：Agent 运行观测报表。

用法：
    python tools/report.py --days 7
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.observability import summarize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 运行观测报表")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    data = summarize(days=args.days)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"观测库: {data['db']}")
    print(f"窗口: 最近 {data['days']} 天")
    print(f"LLM 调用: {data['llm_calls']}，成功: {data['llm_ok']}，"
          f"成功率: {data['llm_success_rate']}")
    print(f"LLM P95: {data['llm_p95_ms']} ms，最大: {data['llm_max_ms']} ms")
    print(f"Provider 分布: {data['llm_providers']}")
    print(f"工具调用: {data['tool_calls']}")
    print(f"Map-reduce: {data['map_reduce_calls']} 次，"
          f"降级: {data['map_reduce_degraded']} 次")
    print(f"交付成功: {data['delivery_ok']}，阻断: {data['delivery_blocked']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
