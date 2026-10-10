#!/bin/bash
# =====================================================
# connect_remote.sh — SSH 隧道连接主机 DSH + LM Studio
#
# 使用场景：移动办公电脑远程访问主机上部署的
#   DSH Web UI (3080) + LM Studio API (1234)
#
# 前提条件：
#   1. 主机已开启「远程登录」（系统设置 → 通用 → 共享）
#   2. 主机上 DSH 和 LM Studio 正在运行
#   3. 两台电脑在同一局域网，或已通过 Tailscale 组网
#
# 用法：
#   bash connect_remote.sh <主机IP或域名> [用户名]
#
# 示例：
#   bash connect_remote.sh 192.168.10.171
#   bash connect_remote.sh simon@192.168.10.171
#   bash connect_remote.sh 100.x.x.x    # Tailscale IP
#
# 连接后浏览器打开 http://localhost:3080
# =====================================================

set -euo pipefail

HOST="${1:-}"
USER="${2:-simon}"

if [ -z "$HOST" ]; then
    echo "用法: bash connect_remote.sh <主机IP或域名> [用户名]"
    echo "示例: bash connect_remote.sh 192.168.10.171"
    echo "      bash connect_remote.sh 100.x.x.x simon  # Tailscale"
    exit 1
fi

# 如果传入的是 user@ip 格式，拆分
if [[ "$HOST" == *"@"* ]]; then
    USER="${HOST%@*}"
    HOST="${HOST#*@}"
fi

echo "===================================================="
echo "  SSH 隧道：连接主机 $USER@$HOST"
echo "===================================================="
echo ""
echo "  转发端口："
echo "    localhost:3080 → 主机 DSH Web UI"
echo "    localhost:1234 → 主机 LM Studio API"
echo "    localhost:8765 → 主机 law-search-cloud (SSE)"
echo ""
echo "  连接后浏览器打开 http://localhost:3080"
echo "  DSH 的 LM Studio provider 保持 http://127.0.0.1:1234/v1 不变"
echo ""
echo "  按 Ctrl+C 断开隧道"
echo "===================================================="
echo ""

# -N: 不执行远程命令，仅做端口转发
# -L: 本地端口转发
# ServerAliveInterval: 30秒心跳保活
ssh -N \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -L 3080:127.0.0.1:3080 \
    -L 1234:127.0.0.1:1234 \
    -L 8765:127.0.0.1:8765 \
    "$USER@$HOST"
