#!/usr/bin/env bash
# ============================================================
# auto_benchmark.sh — 基准测试自动化
#
# 功能：
#   1. 运行 Phase A 检索层基准测试（8 题，无 LLM 调用）
#   2. 对比历史结果，检测性能回归
#   3. 可选：运行 Phase B 端到端测试（5 题，含 LLM 调用）
#   4. 生成 Markdown 报告
#
# 用法：
#   bash auto_benchmark.sh              # 仅 Phase A
#   bash auto_benchmark.sh --full       # Phase A + B
#   bash auto_benchmark.sh --compare    # 与上次结果对比
#
# 定时任务（知识库变更后自动跑分）：
#   在 auto_sync_kb.sh 末尾添加:
#     bash /Users/simon/Documents/1-SLP/MCP/law-search/auto_benchmark.sh --compare
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PYTHON="../../venv/bin/python"
RESULTS_DIR="tests/results"
LATEST_RESULT="$RESULTS_DIR/latest_benchmark.json"
REPORT_FILE="logs/benchmark_report.md"

FULL=0
COMPARE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)     FULL=1; shift ;;
    --compare)  COMPARE=1; shift ;;
    -h|--help)
      head -20 "$0" | tail -16
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ============================================================
# 运行基准测试
# ============================================================
run_benchmark() {
  log "运行基准测试..."
  
  local phases="A"
  if [[ $FULL -eq 1 ]]; then
    phases="A B"
    log "  模式: Phase A + B（含 LLM 调用）"
  else
    log "  模式: Phase A（仅检索层）"
  fi

  # 运行测试
  "$VENV_PYTHON" tests/run_benchmark.py $phases 2>&1 | tee /tmp/benchmark_output.txt

  # 找到最新的结果文件
  local latest_file
  latest_file=$(ls -t "$RESULTS_DIR"/benchmark_phaseA_*.json 2>/dev/null | head -1)
  
  if [[ -z "$latest_file" ]]; then
    log "❌ 未找到测试结果文件"
    return 1
  fi

  # 复制为 latest
  cp "$latest_file" "$LATEST_RESULT"
  log "✅ 结果已保存: $LATEST_RESULT"
}

# ============================================================
# 对比历史结果
# ============================================================
compare_results() {
  local prev_file="$RESULTS_DIR/previous_benchmark.json"
  
  if [[ ! -f "$LATEST_RESULT" ]]; then
    log "无最新结果可对比"
    return 1
  fi

  if [[ ! -f "$prev_file" ]]; then
    log "无历史结果可对比（首次运行）"
    cp "$LATEST_RESULT" "$prev_file"
    return 0
  fi

  log "对比历史结果..."
  
  python3 - "$LATEST_RESULT" "$prev_file" <<'PY'
import json
import sys

latest_file = sys.argv[1]
prev_file = sys.argv[2]

with open(latest_file) as f:
    latest = json.load(f)
with open(prev_file) as f:
    prev = json.load(f)

latest_agg = latest.get("aggregate", {})
prev_agg = prev.get("aggregate", {})

print("\n" + "=" * 60)
print("基准测试对比")
print("=" * 60)

metrics = [
    ("Hit@1_rate", "Hit@1 命中率"),
    ("Hit@3_rate", "Hit@3 命中率"),
    ("avg_relevant_per_query", "平均相关结果数"),
    ("avg_lawhint_match_per_query", "法律来源匹配"),
    ("avg_time_per_query_s", "平均响应时间(s)"),
]

for key, name in metrics:
    latest_val = latest_agg.get(key, 0)
    prev_val = prev_agg.get(key, 0)
    
    if isinstance(latest_val, float):
        diff = latest_val - prev_val
        pct = (diff / prev_val * 100) if prev_val != 0 else 0
        
        if key == "avg_time_per_query_s":
            # 时间越短越好
            status = "✅" if diff < 0 else ("⚠️" if diff > 1 else "➡️")
        else:
            # 其他指标越高越好
            status = "✅" if diff > 0 else ("⚠️" if diff < -0.05 else "➡️")
        
        print(f"  {status} {name:20s}: {prev_val:.4f} → {latest_val:.4f} ({pct:+.1f}%)")
    else:
        print(f"  ➡️  {name:20s}: {prev_val} → {latest_val}")

print("=" * 60)
PY

  # 更新 previous
  cp "$LATEST_RESULT" "$prev_file"
}

# ============================================================
# 生成 Markdown 报告
# ============================================================
generate_report() {
  if [[ ! -f "$LATEST_RESULT" ]]; then
    log "无结果可报告"
    return 1
  fi

  log "生成报告..."
  
  python3 - "$LATEST_RESULT" "$REPORT_FILE" <<'PY'
import json
import sys
from datetime import datetime

result_file = sys.argv[1]
report_file = sys.argv[2]

with open(result_file) as f:
    result = json.load(f)

agg = result.get("aggregate", {})
cases = result.get("cases", [])

lines = [
    "# 基准测试报告",
    "",
    f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    f"**阶段**: {result.get('phase', 'unknown')}",
    f"**总耗时**: {result.get('total_time_s', 0):.1f}s",
    "",
    "## 汇总指标",
    "",
    "| 指标 | 值 |",
    "|------|-----|",
]

for key, val in agg.items():
    name = key.replace("_", " ").title()
    if isinstance(val, float):
        lines.append(f"| {name} | {val:.4f} |")
    else:
        lines.append(f"| {name} | {val} |")

lines.extend([
    "",
    "## 详细结果",
    "",
    "| ID | 领域 | 问题 | Hit@1 | Hit@3 | 相关数 | 时间(s) |",
    "|----|------|------|-------|-------|--------|---------|",
])

for case in cases:
    q = case.get("question", "")[:30]
    hit1 = "✅" if case.get("recall_hit_at_1") else "❌"
    hit3 = "✅" if case.get("recall_hit_at_3") else "❌"
    lines.append(
        f"| {case.get('id', '')} | {case.get('category', '')} | {q}... | "
        f"{hit1} | {hit3} | {case.get('relevant_count_in_topk', 0)} | "
        f"{case.get('time_s', 0):.1f} |"
    )

lines.extend([
    "",
    "---",
    f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
])

with open(report_file, 'w') as f:
    f.write('\n'.join(lines))

print(f"  报告已保存: {report_file}")
PY
}

# ============================================================
# 主流程
# ============================================================
main() {
  log "=========================================="
  log "基准测试自动化开始"
  log "=========================================="

  mkdir -p "$RESULTS_DIR" logs

  # 运行测试
  run_benchmark

  # 对比（如果 --compare）
  if [[ $COMPARE -eq 1 ]]; then
    compare_results
  fi

  # 生成报告
  generate_report

  log "=========================================="
  log "基准测试自动化完成"
  log "=========================================="
}

main
