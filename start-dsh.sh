#!/bin/bash
# ============================================================
# 启动 DSH Desktop：注入 DeepSeek API key + 确保配置正确
# 在自己的终端执行（非 Trae sandbox）：
#   /Users/simon/Documents/1-SLP/start-dsh.sh
# ============================================================
set -e

KEY_FILE="/Users/simon/Documents/1-SLP/MCP/law-search/.secrets/deepseek_api_key"
SETTINGS="$HOME/Library/Application Support/dsh-desktop/harness/settings.yaml"
CRED_FILE="$HOME/Library/Application Support/dsh-desktop/harness/.credentials.yaml"

if [ ! -f "$KEY_FILE" ]; then
  echo "ERROR: API key file not found: $KEY_FILE"
  exit 1
fi

# 1. 退出 DSH（如果在运行）
echo "[1/5] Quitting DSH Desktop if running..."
osascript -e 'quit app "DSH Desktop"' 2>/dev/null || true
sleep 2

# 2. 确保 settings.yaml 的 agent-default-model 是 deepseek-official
#    （防止旧标签页覆盖回 lm-studio）
echo "[2/5] Ensuring settings.yaml has deepseek-official..."
python3 -c "
import re
with open('$SETTINGS') as f:
    content = f.read()
content = re.sub(
    r'(agent-default-model:\n  provider: )\S+\n(  model: )\S+',
    r'\1deepseek-official\n\2deepseek-v4-flash',
    content
)
with open('$SETTINGS', 'w') as f:
    f.write(content)
print('  settings.yaml: deepseek-official confirmed')
"

# 3. 写 API key 到 .credentials.yaml 的 refs 段
#    （DSH credentials resolve 第二层，不需要 launchctl）
echo "[3/5] Writing DEEPSEEK_API_KEY to .credentials.yaml..."
python3 -c "
import yaml, os
with open('$KEY_FILE') as f:
    api_key = f.read().strip()
with open('$CRED_FILE') as f:
    doc = yaml.safe_load(f) or {}
doc.setdefault('refs', {})
doc['refs']['DEEPSEEK_API_KEY'] = api_key
with open('$CRED_FILE', 'w') as f:
    yaml.dump(doc, f, default_flow_style=False, sort_keys=False)
os.chmod('$CRED_FILE', 0o600)
print('  .credentials.yaml: DEEPSEEK_API_KEY written (refs count: %d)' % len(doc.get('refs',{})))
"

# 4. 启动 DSH Desktop
echo "[4/5] Starting DSH Desktop..."
open -a "DSH Desktop"

# 5. 提醒
echo "[5/5] Done!"
echo ""
echo "============================================"
echo "  IMPORTANT: Browser hard refresh Cmd+Shift+R"
echo "  after DSH loads (prevents old tab form"
echo "  state from overwriting settings.yaml)"
echo "============================================"
