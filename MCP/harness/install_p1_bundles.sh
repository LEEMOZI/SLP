#!/bin/bash
# 安装 P1 插件到 DSH Desktop
# 在真实终端执行：bash /Users/simon/Documents/1-SLP/MCP/harness/install_p1_bundles.sh

set -e

PLUGINS_DIR="/Users/simon/Documents/1-SLP/MCP/harness/plugins"
DSH_DESKTOP_HARNESS="/Users/simon/Library/Application Support/dsh-desktop/harness"

echo "=== 安装 P1 插件到 DSH Desktop ==="

# 1. 创建符号链接
WEB_NM="$DSH_DESKTOP_HARNESS/profiles/web/node_modules/@law-agent"
mkdir -p "$WEB_NM"

ln -sf "$PLUGINS_DIR/skill-provider-bundle" "$WEB_NM/skill-provider-bundle"
ln -sf "$PLUGINS_DIR/session-storage-bundle" "$WEB_NM/session-storage-bundle"

echo "✅ 符号链接创建完成"
ls -la "$WEB_NM/"

# 2. 更新 web profile 的 package.json 添加两个新 bundle
WEB_PKG="$DSH_DESKTOP_HARNESS/profiles/web/package.json"
if grep -q "skill-provider-bundle" "$WEB_PKG"; then
  echo "✅ web profile package.json 已包含 P1 插件"
else
  # 用 node 更新 JSON（保留格式）
  node -e "
    const fs = require('fs');
    const pkg = JSON.parse(fs.readFileSync('$WEB_PKG', 'utf8'));
    pkg.dsh.profile.bundles.push('@law-agent/skill-provider-bundle');
    pkg.dsh.profile.bundles.push('@law-agent/session-storage-bundle');
    fs.writeFileSync('$WEB_PKG', JSON.stringify(pkg, null, 2) + '\n');
    console.log('✅ web profile package.json 已更新');
  "
fi

# 3. 更新 lm-studio-workbench profile 的 package.json
WB_PKG="$DSH_DESKTOP_HARNESS/profiles/lm-studio-workbench/package.json"
if [ -f "$WB_PKG" ]; then
  if grep -q "skill-provider-bundle" "$WB_PKG"; then
    echo "✅ lm-studio-workbench profile 已包含 P1 插件"
  else
    WB_NM="$DSH_DESKTOP_HARNESS/profiles/lm-studio-workbench/node_modules/@law-agent"
    mkdir -p "$WB_NM"
    ln -sf "$PLUGINS_DIR/skill-provider-bundle" "$WB_NM/skill-provider-bundle"
    ln -sf "$PLUGINS_DIR/session-storage-bundle" "$WB_NM/session-storage-bundle"

    node -e "
      const fs = require('fs');
      const pkg = JSON.parse(fs.readFileSync('$WB_PKG', 'utf8'));
      pkg.dsh.profile.bundles.push('@law-agent/skill-provider-bundle');
      pkg.dsh.profile.bundles.push('@law-agent/session-storage-bundle');
      fs.writeFileSync('$WB_PKG', JSON.stringify(pkg, null, 2) + '\n');
      console.log('✅ lm-studio-workbench profile package.json 已更新');
    "
  fi
fi

# 4. 确认 sessions 目录存在
SESSIONS_DIR="/Users/simon/Documents/1-SLP/MCP/harness/sessions"
mkdir -p "$SESSIONS_DIR"
echo "✅ 会话存储目录: $SESSIONS_DIR"

echo ""
echo "=== 安装完成 ==="
echo "请重启 DSH Desktop（Cmd+Q 后重新打开）使插件生效。"
echo "验证：新开对话时检查是否出现法律领域指南注入。"
