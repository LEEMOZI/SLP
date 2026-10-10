#!/usr/bin/env bash
# ============================================================
# run_cloud.sh — 启动 law-search-cloud（SSE/HTTP + Bearer Token 鉴权）
#
# ⚠️ 注意（S1 已修，2026-09-03）：
#   --transport http 也会正确走 ASGI + Starlette BearerTokenAuthMiddleware，
#   不再出现『配置了 token 但实际裸奔』的静默绕过。
#
# 用法：
#   bash run_cloud.sh                    # Streamable HTTP + 自动生成 token（默认）
#   bash run_cloud.sh --transport sse    # SSE transport（兼容旧客户端）
#   bash run_cloud.sh --no-auth          # 不启用鉴权（仅限 loopback 调试）
#   bash run_cloud.sh --host 0.0.0.0     # 监听所有网卡（远程访问）
#   bash run_cloud.sh --port 9000        # 自定义端口
#   bash run_cloud.sh --token mysecret   # 固定 token（与 server.py --auth-token 等价）
#
# 环境变量：
#   LAW_SEARCH_CLOUD_TOKEN  — 预设 token（优先级低于 --token）
#   LAW_SEARCH_CLOUD_PORT   — 端口（默认 8765）
#   LAW_SEARCH_CLOUD_HOST   — 监听地址（默认 127.0.0.1）
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

VENV_PYTHON="${SCRIPT_DIR}/../../venv/bin/python"
TOKEN_DIR="${SCRIPT_DIR}/.secrets"
TOKEN_FILE="${TOKEN_DIR}/cloud_auth_token"
mkdir -p "${TOKEN_DIR}"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "❌ [run_cloud.sh] 找不到 venv Python: ${VENV_PYTHON}" >&2
  echo "   SCRIPT_DIR=${SCRIPT_DIR}" >&2
  echo "   请确认: /Users/simon/Documents/1-SLP/venv/bin/python 存在" >&2
  exit 1
fi

# 默认值
TRANSPORT="http"
HOST="${LAW_SEARCH_CLOUD_HOST:-127.0.0.1}"
PORT="${LAW_SEARCH_CLOUD_PORT:-8765}"
AUTH_TOKEN="${LAW_SEARCH_CLOUD_TOKEN:-auto}"
NO_AUTH=0
NO_PRECHECK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --transport)  TRANSPORT="$2"; shift 2 ;;
    --host)       HOST="$2"; shift 2 ;;
    --port)       PORT="$2"; shift 2 ;;
    --token)      AUTH_TOKEN="$2"; shift 2 ;;
    --no-auth)    NO_AUTH=1; shift ;;
    --no-precheck) NO_PRECHECK=1; shift ;;
    -h|--help)
      head -20 "$0" | tail -16
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

# 检查 Python
if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "❌ [run_cloud.sh] 找不到 venv Python: $VENV_PYTHON" >&2
  echo "   当前 SCRIPT_DIR=${SCRIPT_DIR}" >&2
  echo "   请确认存在: /Users/simon/Documents/1-SLP/venv/bin/python" >&2
  echo "   或进入 law-search 目录后再执行: cd /Users/simon/Documents/1-SLP/MCP/law-search" >&2
  exit 1
fi

# 构建启动命令（server.py 接受 --auth-token / --auth-token-file；
# 但 run_cloud.sh 对外暴露 --token 以保持与文档一致，server.py 侧做别名兼容）
ARGS=(
  "$VENV_PYTHON"
  "${SCRIPT_DIR}/server.py"
  "--transport" "$TRANSPORT"
  "--host" "$HOST"
  "--port" "$PORT"
)

if [[ $NO_AUTH -eq 0 ]]; then
  ARGS+=("--auth-token" "$AUTH_TOKEN" "--auth-token-file" "$TOKEN_FILE")
fi

echo "============================================"
echo " law-search-cloud 启动"
echo "============================================"
echo "  Transport:  ${TRANSPORT}"
echo "  监听地址:   ${HOST}:${PORT}"
if [[ ${NO_AUTH} -eq 0 ]]; then
  echo "  鉴权:       Bearer Token（token 文件: ${TOKEN_FILE}）"
  if [[ "${AUTH_TOKEN}" == "auto" ]]; then
    echo "  Token:      自动生成（启动后写入 ${TOKEN_FILE} 并打印）"
  else
    echo "  Token:      用户指定（len=${#AUTH_TOKEN}）"
  fi
else
  echo "  鉴权:       无（仅限 loopback 调试）"
fi
echo "============================================"
echo

# ------------------------------------------------------------
# 预检（鉴权模式下，启动后 3 秒内对 401 做一次自测，避免鉴权静默失效）
# ------------------------------------------------------------
do_precheck() {
  local wait_s="$1"
  local endpoint_path="/mcp"
  [[ "$TRANSPORT" == "sse" ]] && endpoint_path="/sse"
  local url="http://${HOST}:${PORT}${endpoint_path}"

  sleep "$wait_s"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$url" 2>/dev/null || echo "000")
  echo
  if [[ "$code" == "401" ]]; then
    echo "[precheck ✅] 无 token 访问返回 401，鉴权中间件正常挂载。"
  elif [[ "$code" == "000" ]]; then
    echo "[precheck ⏳] 服务仍在启动（连接拒绝为正常，uvicorn 会继续）。若 10s 后仍无响应请检查 logs/server.log。"
  else
    echo "[precheck 🔴] 无 token 访问返回 $code（预期 401）！请立刻终止进程并排查鉴权代码。"
  fi
}

if [[ $NO_AUTH -eq 0 && $NO_PRECHECK -eq 0 ]]; then
  do_precheck 3 &
  PRECHECK_PID=$!
fi

# 启动服务（exec 替换当前进程，PID 1 继承信号）
trap '[[ -n "${PRECHECK_PID:-}" ]] && kill "$PRECHECK_PID" 2>/dev/null || true' EXIT
exec "${ARGS[@]}"
