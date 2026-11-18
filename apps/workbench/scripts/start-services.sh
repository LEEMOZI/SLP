#!/usr/bin/env bash
# 由 SLP 法律工作台桌面端（Tauri）调用：启动 sidecar(8790) + workbench(8780)，前台阻塞。
# 端口已被占用时跳过对应服务（兼容 start.sh 已启动的开发环境）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKBENCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$WORKBENCH_DIR/../.." && pwd)"

# 兼容 .venv 和 venv 两种路径
if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  VENV="$PROJECT_ROOT/.venv/bin/python"
else
  VENV="$PROJECT_ROOT/venv/bin/python"
fi

if [[ -f "$WORKBENCH_DIR/.env" ]]; then
  set -a; source "$WORKBENCH_DIR/.env"; set +a
fi
export SLP_CAPABILITY_ADAPTER_TOKEN="${SLP_CAPABILITY_ADAPTER_TOKEN:-local-internal-token}"

PIDS=()
port_in_use() { lsof -i "127.0.0.1:$1" -sTCP:LISTEN >/dev/null 2>&1; }

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null
  done
  wait 2>/dev/null
}
trap cleanup EXIT INT TERM

if ! port_in_use 8790; then
  "$VENV" -m uvicorn apps.capability_adapter.app:app \
    --host 127.0.0.1 --port 8790 --app-dir "$PROJECT_ROOT" &
  PIDS+=($!)
fi
sleep 2
if ! port_in_use 8780; then
  "$VENV" -m uvicorn apps.workbench.backend.app:app \
    --host 127.0.0.1 --port 8780 --app-dir "$PROJECT_ROOT" &
  PIDS+=($!)
fi
wait
