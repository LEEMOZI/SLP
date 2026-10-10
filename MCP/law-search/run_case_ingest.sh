#!/bin/bash
# =============================================================
# case_library 一键入库 v2：诊断 + ingest + 日志落盘
# 用法：bash run_case_ingest.sh
# =============================================================
cd "$(dirname "$0")"

LOG="logs/case_ingest_last.log"
mkdir -p logs

{
echo "===== [诊断 1] 文件在真实终端是否可见 ====="
TARGET="/Users/simon/Desktop/Laws&Rules/最高人民法院指导性案例全集（截止至2024年5月30日第40批）.docx"
ls -la "/Users/simon/Desktop/Laws&Rules/" | head -20
echo "---"
ls -la "$TARGET" 2>&1

echo ""
echo "===== [诊断 2] docx 可读性与段落数 ====="
/Users/simon/Documents/1-SLP/venv/bin/python - "$TARGET" <<'EOF'
import sys
from docx import Document
try:
    doc = Document(sys.argv[1])
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    print(f"OK: {len(paras)} 个非空段落，总字数 {sum(len(t) for t in paras)}")
    print("首段预览:", paras[0][:80] if paras else "(无)")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")
EOF

echo ""
echo "===== [ingest] case_library ====="
/Users/simon/Documents/1-SLP/venv/bin/python ingest.py --library case_library 2>&1

echo ""
echo "===== [验证] chunks ====="
/Users/simon/Documents/1-SLP/venv/bin/python - <<'EOF'
import chromadb
c = chromadb.PersistentClient(path="chroma")
for x in c.list_collections():
    print(f"  {x.name}: {x.count()} chunks")
EOF
} 2>&1 | tee "$LOG"

echo ""
echo "完整日志已保存: $LOG"
