#!/bin/bash
# ============================================================
# LM Studio 推理参数优化 — 上下文长度调整
# 生成时间: 2026-10-24
#
# 作用: 把主力模型 qwen3.6-35b-a3b 的 loaded_context_length 设为指定值
#       （默认 128K）；降至 32K/16K 可显著降低 KV cache 内存与注意力开销。
#       质量备用 qwen3.8-27b 可用 MODEL=qwen/qwen3.8-27b 覆盖。
#
# 收益预估:
#   128K -> 32K   KV cache 内存约降 75%，长对话 prefill 明显加快
#   128K -> 16K   KV cache 内存约降 87%，适合纯工具调用型 Agent 任务
#
# 警告: 重载期间 (:1234) 会短暂中断 30~60 秒，
#       请勿在长任务执行过程中运行本脚本。
# ============================================================

set -u

MODEL="${MODEL:-qwen/qwen3.6-35b-a3b}"
CTX="${CTX:-131072}"
API="http://127.0.0.1:1234"

echo "目标模型      : $MODEL"
echo "目标上下文长度: $CTX tokens"
echo

echo "=== 当前状态 ==="
curl -s -m 10 "$API/api/v0/models" | python3 -c "
import sys, json
for m in json.load(sys.stdin).get('data', []):
    print(f\"  {m.get('id')}: {m.get('state')}  loaded_ctx={m.get('loaded_context_length')} max_ctx={m.get('max_context_length')}\")
"

echo
read -r -p "确认重载该模型？(y/N) " ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "已取消"; exit 0; }

echo
echo "=== 正在重载 (请稍候 30~60 秒) ==="
curl -s -m 300 -X POST "$API/api/v0/models/load" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"context_length\":$CTX}" \
  -w "\n[http=%{http_code} time=%{time_total}s]\n"

echo
echo "=== 重载后状态 ==="
curl -s -m 10 "$API/api/v0/models" | python3 -c "
import sys, json
for m in json.load(sys.stdin).get('data', []):
    print(f\"  {m.get('id')}: {m.get('state')}  loaded_ctx={m.get('loaded_context_length')}\")
"

echo
echo "=== 内存快照 ==="
vm_stat | grep -E "Pages free|Pages wired down"
echo "wired(GiB): $(echo "scale=2; $(vm_stat | awk '/wired down/{print $4}' | tr -d '.') * 16384 / 1073741824" | bc)"
