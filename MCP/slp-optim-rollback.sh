#!/bin/bash
# ============================================================
# Mac Studio 性能优化 — 回滚 / 切换工具
# 生成时间: 2026-10-24
# 说明: 所有改动均可逆，此脚本提供快速回滚与 A/B 切换能力
# ============================================================

set -u
MCP="$HOME/.workbuddy/mcp.json"
LA="$HOME/Library/LaunchAgents"

usage() {
  cat <<EOF
用法: $0 <命令>

  restore-mcp        回滚 mcp.json 到修改前的备份
  enable-cloud       重新启用 law-search-cloud (HTTP 8765) 常驻服务
  disable-cloud      停用 law-search-cloud (HTTP 8765) 常驻服务
  status             查看当前 MCP 服务运行状态
  bench              快速性能快照（内存/swap/端口）

提示:
  law-search (stdio)        每次会话重新加载模型，冷启动约 3 分钟
  law-search-cloud (HTTP)   常驻且已预热，工具调用秒回，但常驻占用约 16.5GB 内存
EOF
}

restore_mcp() {
  BAK=$(ls -t "$HOME"/.workbuddy/mcp.json.bak.* 2>/dev/null | head -1)
  if [ -n "$BAK" ]; then
    cp "$MCP" "$MCP.before-restore.$(date +%s)"
    cp "$BAK" "$MCP"
    echo "✓ 已从 $BAK 回滚 mcp.json"
  else
    echo "✗ 未找到备份文件"
  fi
}

enable_cloud() {
  if [ -f "$LA/com.law-agent.cloud.plist.disabled" ]; then
    mv "$LA/com.law-agent.cloud.plist.disabled" "$LA/com.law-agent.cloud.plist"
    echo "✓ 已恢复 LaunchAgent 配置"
  fi
  launchctl bootstrap gui/$(id -u) "$LA/com.law-agent.cloud.plist" 2>/dev/null
  launchctl kickstart -k gui/$(id -u)/com.law-agent.cloud 2>/dev/null
  sleep 3
  nc -z -w 3 127.0.0.1 8765 && echo "✓ 8765 已恢复监听" || echo "⚠ 8765 未就绪，请检查日志"
}

disable_cloud() {
  launchctl bootout gui/$(id -u)/com.law-agent.cloud 2>/dev/null
  [ -f "$LA/com.law-agent.cloud.plist" ] && mv "$LA/com.law-agent.cloud.plist" "$LA/com.law-agent.cloud.plist.disabled"
  sleep 2
  nc -z -w 3 127.0.0.1 8765 && echo "⚠ 仍在监听" || echo "✓ 8765 已停用"
}

show_status() {
  echo "=== MCP 常驻服务端口 ==="
  for p in 8765 8766 8767; do
    nc -z -w 2 127.0.0.1 $p && echo "  $p  在线" || echo "  $p  离线"
  done
  echo
  echo "=== LM Studio (:1234) ==="
  nc -z -w 2 127.0.0.1 1234 && echo "  在线" || echo "  离线"
  echo
  echo "=== mcp.json 备份 ==="
  ls -1 "$HOME"/.workbuddy/mcp.json.bak.* 2>/dev/null | tail -3 || echo "  无"
}

bench() {
  echo "=== 内存快照 ==="
  vm_stat | grep -E "Pages free|Pages wired down|Pages active"
  echo "wired(GiB): $(echo "scale=2; $(vm_stat | awk '/wired down/{print $4}' | tr -d '.') * 16384 / 1073741824" | bc)"
  echo "free(MiB) : $(echo "scale=1; $(vm_stat | awk '/^Pages free/{print $3}' | tr -d '.') * 16384 / 1048576" | bc)"
  echo
  echo "=== Swap ==="
  sysctl -n vm.swapusage
  echo
  echo "=== 负载 ==="
  uptime
}

case "${1:-}" in
  restore-mcp)    restore_mcp ;;
  enable-cloud)   enable_cloud ;;
  disable-cloud)  disable_cloud ;;
  status)         show_status ;;
  bench)          bench ;;
  *)              usage ;;
esac
