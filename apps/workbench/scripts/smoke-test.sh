#!/usr/bin/env bash
# P1 端到端冒烟测试：验证 sidecar 三条 LLM 生成链路（摘要/合同审查/诉讼文书）
# 用法：bash apps/workbench/scripts/smoke-test.sh
# 前提：LM Studio 代理已在 127.0.0.1:1235 运行
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  VENV="$PROJECT_ROOT/.venv/bin/python"
else
  VENV="$PROJECT_ROOT/venv/bin/python"
fi

export SLP_CAPABILITY_ADAPTER_TOKEN="${SLP_CAPABILITY_ADAPTER_TOKEN:-local-internal-token}"
SIDECAR_URL="http://127.0.0.1:8790"
TOKEN="$SLP_CAPABILITY_ADAPTER_TOKEN"

PASS=0
FAIL=0
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null
  done
  wait 2>/dev/null
}
trap cleanup EXIT INT TERM

port_in_use() { lsof -i "127.0.0.1:$1" -sTCP:LISTEN >/dev/null 2>&1; }

wait_for_port() {
  local port=$1 name=$2 max_wait=${3:-30}
  for i in $(seq 1 "$max_wait"); do
    if port_in_use "$port"; then
      echo "  ✓ $name 已就绪 (端口 $port)"
      return 0
    fi
    sleep 1
  done
  echo "  ✗ $name 启动超时 (端口 $port)"
  return 1
}

check() {
  local name=$1 url=$2 payload=$3 expect_status=$4
  local response status body
  response=$(curl -s -w "\n%{http_code}" -X POST "$url" \
    -H "Content-Type: application/json" \
    -H "X-SLP-Internal-Token: $TOKEN" \
    -d "$payload" 2>/dev/null)
  status=$(echo "$response" | tail -1)
  body=$(echo "$response" | head -n -1)

  if [[ "$status" == "$expect_status" ]]; then
    echo "  ✓ $name → $status"
    PASS=$((PASS + 1))
  else
    echo "  ✗ $name → $status (期望 $expect_status)"
    echo "    响应: $(echo "$body" | head -c 300)"
    FAIL=$((FAIL + 1))
  fi
}

echo "=========================================="
echo " SLP P1 端到端冒烟测试"
echo "=========================================="
echo ""

# --- 0. 检查 LM Studio 代理 ---
echo "[0/4] 检查 LM Studio 代理 (127.0.0.1:1235)..."
if port_in_use 1235; then
  echo "  ✓ LM Studio 代理在线"
else
  echo "  ✗ LM Studio 代理未运行，请先启动 LM Studio 并加载模型"
  echo "    提示: 确保代理脚本监听 127.0.0.1:1235"
  exit 1
fi
echo ""

# --- 1. 启动 sidecar ---
echo "[1/4] 启动 capability_adapter (端口 8790)..."
if port_in_use 8790; then
  echo "  ✓ 已在运行，跳过启动"
else
  "$VENV" -m uvicorn apps.capability_adapter.app:app \
    --host 127.0.0.1 --port 8790 --app-dir "$PROJECT_ROOT" &
  PIDS+=($!)
  wait_for_port 8790 "capability_adapter" 15 || exit 1
fi
echo ""

# --- 2. 检查 capabilities ---
echo "[2/4] 检查 /v1/capabilities..."
CAPS=$(curl -s "$SIDECAR_URL/v1/capabilities" 2>/dev/null)
if echo "$CAPS" | grep -q '"version"'; then
  echo "  ✓ capabilities 正常"
  SUMMARY_STATUS=$(echo "$CAPS" | "$VENV" -c "import sys,json; d=json.load(sys.stdin); print(d['summary']['providers'][1]['status'])" 2>/dev/null)
  echo "    openai-compatible 状态: $SUMMARY_STATUS"
else
  echo "  ✗ capabilities 请求失败"
  FAIL=$((FAIL + 1))
fi
echo ""

# --- 3. 三条生成链路 ---
echo "[3/4] 测试 LLM 生成链路..."
echo ""

# 3a. 摘要
echo "  → 摘要 (openai-compatible)..."
SUMMARY_PAYLOAD='{"question":"该合同的主要风险点是什么？","sources":[{"handle":"S1","title":"测试合同","text":"甲方应向乙方支付货款人民币100万元。乙方应在收到货款后30日内交付货物。如乙方逾期交付，每日按合同总价的0.5%支付违约金。"}],"provider":"openai-compatible"}'
check "摘要生成" "$SIDECAR_URL/v1/summaries" "$SUMMARY_PAYLOAD" "200"

# 3b. 合同审查
echo "  → 合同审查 (openai-compatible)..."
REVIEW_PAYLOAD='{"material_text":"甲方（买方）：张三\n乙方（卖方）：李四\n第一条 甲方应向乙方支付货款人民币50万元。\n第二条 乙方应在收到货款后15日内交付全部货物。\n第三条 如乙方逾期交付，每日按合同总价的1%支付违约金。\n第四条 本合同自双方签字之日起生效。","contract_type":"买卖合同","focus_points":"违约金是否过高","top_k":3,"quality":false}'
check "合同审查" "$SIDECAR_URL/v1/reviews" "$REVIEW_PAYLOAD" "200"

# 3c. 诉讼文书
echo "  → 诉讼文书 (openai-compatible)..."
LITIGATION_PAYLOAD='{"doc_type":"complaint","title":"买卖合同纠纷起诉状","parties":"原告：张三，男，1980年1月1日出生，住北京市朝阳区。被告：李四，男，1985年5月1日出生，住上海市浦东新区。","claims":"1. 判令被告支付货款人民币50万元；2. 判令被告支付逾期违约金；3. 本案诉讼费由被告承担。","facts":"2024年3月1日，原被告签订买卖合同，约定被告向原告供应货物，总价50万元。原告已按约支付全部货款，但被告至今未交付货物。","focus_points":"","top_k":3,"quality":false}'
check "诉讼文书" "$SIDECAR_URL/v1/litigation" "$LITIGATION_PAYLOAD" "200"

echo ""

# --- 4. 降级测试（可选，仅当 LM Studio 代理关闭时验证）---
echo "[4/4] 降级策略验证..."
echo "  （跳过：需手动关闭 LM Studio 代理后重跑）"
echo ""

# --- 汇总 ---
echo "=========================================="
echo " 结果: $PASS 通过, $FAIL 失败"
echo "=========================================="

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
echo " 全部通过 ✓"
