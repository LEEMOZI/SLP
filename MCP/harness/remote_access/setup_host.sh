#!/usr/bin/env bash
# ============================================================
# setup_host.sh — 主机端一键部署（远程访问 + 服务自启动）
#
# 功能：
#   1. 开启 SSH 远程登录（macOS 系统设置）
#   2. 配置防火墙允许 SSH（/usr/libexec/ApplicationFirewall/socketfilterfw）
#   3. 安装 Tailscale（可选，需 brew）
#   4. 创建 launchd plist 自启动 law-search-cloud（SSE + 鉴权）
#   5. 验证服务连通
#
# 用法：
#   bash setup_host.sh                    # 全部执行
#   bash setup_host.sh --skip-tailscale   # 跳过 Tailscale 安装
#   bash setup_host.sh --dry-run          # 仅打印操作，不执行
#
# 前提：macOS + Homebrew + 本项目已 clone 到 ~/Documents/1-SLP
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
LAW_SEARCH_DIR="$PROJECT_DIR/MCP/law-search"
PLIST_NAME="com.law-agent.cloud"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$PLIST_NAME.plist"
LOG_DIR="$LAW_SEARCH_DIR/logs"
TOKEN_DIR="$LAW_SEARCH_DIR/.secrets"

DRY_RUN=0
SKIP_TAILSCALE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)         DRY_RUN=1; shift ;;
    --skip-tailscale)  SKIP_TAILSCALE=1; shift ;;
    -h|--help)
      head -20 "$0" | tail -16
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

run() {
  local desc="$1"; shift
  echo "  · $desc"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "    (dry-run) $*"
  else
    "$@"
  fi
}

echo "===================================================="
echo "  主机端部署 · law-search-cloud"
echo "===================================================="
echo "  项目目录: $PROJECT_DIR"
echo "  模式:     $( [[ $DRY_RUN -eq 1 ]] && echo 'DRY-RUN' || echo 'APPLY' )"
echo "===================================================="
echo

# ------------------------------------------------------------
# 1. 开启 SSH 远程登录
# ------------------------------------------------------------
echo "[1/5] SSH 远程登录..."
if systemsetup -getremotelogin 2>/dev/null | grep -q "On"; then
  echo "  · SSH 已开启 ✅"
else
  echo "  · 开启 SSH 远程登录..."
  run "sudo systemsetup -setremotelogin on" sudo systemsetup -setremotelogin on || \
    echo "  ⚠️  请手动开启：系统设置 → 通用 → 共享 → 远程登录"
fi

# ------------------------------------------------------------
# 2. 防火墙配置
# ------------------------------------------------------------
echo "[2/5] 防火墙配置..."
if /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null | grep -q "disabled"; then
  echo "  · 防火墙未启用，跳过"
else
  # 允许 SSH 通过防火墙
  run "允许 SSH 通过防火墙" \
    sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add /usr/sbin/sshd 2>/dev/null || true
  run "允许 Python 通过防火墙（law-search-cloud）" \
    sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$(which python3)" 2>/dev/null || true
  echo "  · 防火墙规则已配置"
fi

# ------------------------------------------------------------
# 3. Tailscale（可选）
# ------------------------------------------------------------
echo "[3/5] Tailscale..."
if [[ $SKIP_TAILSCALE -eq 1 ]]; then
  echo "  · 已跳过（--skip-tailscale）"
elif command -v tailscale &>/dev/null; then
  TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "未连接")
  echo "  · Tailscale 已安装 ✅ IP: $TAILSCALE_IP"
else
  echo "  · 安装 Tailscale..."
  if command -v brew &>/dev/null; then
    run "brew install tailscale" brew install tailscale
    echo "  · 请手动执行 'tailscale up' 完成登录"
  else
    echo "  ⚠️  Homebrew 未安装，请手动安装 Tailscale: https://tailscale.com/download/mac"
  fi
fi

# ------------------------------------------------------------
# 4. launchd 自启动 law-search-cloud
# ------------------------------------------------------------
echo "[4/5] launchd 自启动配置..."
mkdir -p "$LOG_DIR" "$TOKEN_DIR" "$PLIST_DIR"

# 生成 token（如果不存在）
TOKEN_FILE="$TOKEN_DIR/cloud_auth_token"
if [[ ! -f "$TOKEN_FILE" ]]; then
  if [[ $DRY_RUN -eq 0 ]]; then
    python3 -c "import secrets; print(secrets.token_hex(32))" > "$TOKEN_FILE"
  fi
  echo "  · 将生成鉴权 token: $TOKEN_FILE"
else
  echo "  · 鉴权 token 已存在: $TOKEN_FILE"
fi

# 读取 token 内容（plist 需要内联字面值，launchd 不支持 file: 前缀约定）
ACTUAL_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null || echo "")

# 写入 plist
if [[ $DRY_RUN -eq 0 && -n "$ACTUAL_TOKEN" ]]; then
  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/venv/bin/python</string>
        <string>$LAW_SEARCH_DIR/server.py</string>
        <string>--transport</string>
        <string>http</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8765</string>
        <string>--auth-token</string>
        <string>$ACTUAL_TOKEN</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$LAW_SEARCH_DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>TOKENIZERS_PARALLELISM</key>
        <string>false</string>
        <key>LLM_PROVIDER</key>
        <string>auto</string>
        <key>PATH</key>
        <string>$(dirname "$PROJECT_DIR/venv/bin"):/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/cloud-stdout.log</string>

    <key>StandardErrorPath</key>
    <string>$LOG_DIR/cloud-stderr.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST
  echo "  · plist 已写入: $PLIST_PATH"
else
  echo "  · (dry-run) 将写入 plist: $PLIST_PATH"
fi

if [[ $DRY_RUN -eq 0 ]]; then
  # 卸载旧服务（如果存在）— macOS 13+ 使用 bootout 替代已弃用的 unload
  launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true
  # 加载新服务
  launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
  echo "  · 服务已启动 ✅"
else
  echo "  · (dry-run) launchctl bootstrap gui/$(id -u) $PLIST_PATH"
fi

# ------------------------------------------------------------
# 5. 验证连通
# ------------------------------------------------------------
echo "[5/5] 验证连通..."
sleep 3

# 检查服务是否在运行
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/ --max-time 3 2>/dev/null | grep -qE "^(200|401|404)"; then
  echo "  · law-search-cloud 已启动 ✅ (http://127.0.0.1:8765)"
else
  echo "  ⚠️  law-search-cloud 未响应，请检查日志: $LOG_DIR/cloud-stderr.log"
fi

# 显示 token
if [[ -f "$TOKEN_FILE" ]]; then
  echo "  · 鉴权 token: $(cat "$TOKEN_FILE")"
  echo "  · 客户端连接命令:"
  echo "    curl -H 'Authorization: Bearer $(cat "$TOKEN_FILE")' http://127.0.0.1:8765/mcp"
fi

echo
echo "===================================================="
echo "  部署完成"
echo "===================================================="
echo
echo "  远程连接（从另一台电脑）："
echo "    bash connect_remote.sh <主机IP或Tailscale IP>"
echo
echo "  管理命令："
echo "    launchctl bootout gui/\$(id -u)/$PLIST_NAME   # 停止服务"
echo "    launchctl bootstrap gui/\$(id -u) \"$PLIST_PATH\"  # 启动服务"
echo "    tail -f $LOG_DIR/cloud-stderr.log  # 查看日志"
echo
if [[ $DRY_RUN -eq 1 ]]; then
  echo "  ⚠️  当前是 DRY-RUN 模式，未真正执行。重跑："
  echo "      bash $0"
fi
