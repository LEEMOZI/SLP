#!/usr/bin/env bash
# SLP 法律工作台 — 本地开发启动脚本
# 用法: ./start.sh [sidecar|workbench|frontend|all]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV="$PROJECT_ROOT/.venv/bin/python"
ENV_FILE="$SCRIPT_DIR/.env"

# 加载 .env
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

# 默认 token
export SLP_CAPABILITY_ADAPTER_TOKEN="${SLP_CAPABILITY_ADAPTER_TOKEN:-local-internal-token}"

start_sidecar() {
  echo "==> 启动 Capability Adapter sidecar (127.0.0.1:8790)"
  "$VENV" -m uvicorn apps.capability_adapter.app:app \
    --host 127.0.0.1 --port 8790 \
    --app-dir "$PROJECT_ROOT" &
  SIDECAR_PID=$!
  echo "    PID: $SIDECAR_PID"
}

start_workbench() {
  echo "==> 启动工作台后端 (127.0.0.1:8780)"
  "$VENV" -m uvicorn apps.workbench.backend.app:app \
    --host 127.0.0.1 --port 8780 \
    --app-dir "$PROJECT_ROOT" &
  WORKBENCH_PID=$!
  echo "    PID: $WORKBENCH_PID"
}

start_frontend() {
  echo "==> 启动前端开发服务器 (127.0.0.1:5173)"
  cd "$SCRIPT_DIR/frontend"
  npm run dev &
  FRONTEND_PID=$!
  echo "    PID: $FRONTEND_PID"
}

cleanup() {
  echo ""
  echo "==> 停止所有服务…"
  for pid in ${SIDECAR_PID:-} ${WORKBENCH_PID:-} ${FRONTEND_PID:-}; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
  echo "==> 已停止"
}
trap cleanup EXIT INT TERM

TARGET="${1:-all}"

case "$TARGET" in
  sidecar)
    start_sidecar
    wait
    ;;
  workbench)
    start_workbench
    wait
    ;;
  frontend)
    start_frontend
    wait
    ;;
  all)
    start_sidecar
    sleep 2
    start_workbench
    sleep 1
    start_frontend
    echo ""
    echo "=========================================="
    echo "  SLP 法律工作台已启动"
    echo "  前端:     http://127.0.0.1:5173"
    echo "  后端 API: http://127.0.0.1:8780"
    echo "  Sidecar:  http://127.0.0.1:8790"
    echo "  LLM 代理: http://127.0.0.1:1235/v1"
    echo "=========================================="
    wait
    ;;
  *)
    echo "用法: $0 [sidecar|workbench|frontend|all]"
    exit 1
    ;;
esac
