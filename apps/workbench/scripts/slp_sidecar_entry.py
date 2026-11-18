#!/usr/bin/env python3
"""SLP Sidecar - PyInstaller 入口点。

打包命令（项目根目录执行）:
  pyinstaller --name slp-sidecar --onefile --paths . \
    --hidden-import apps.capability_adapter \
    --hidden-import apps.capability_adapter.app \
    --hidden-import apps.capability_adapter.parser \
    --hidden-import apps.capability_adapter.retrieval \
    --hidden-import apps.capability_adapter.judgments \
    --hidden-import apps.capability_adapter.summarizer \
    --hidden-import apps.capability_adapter.review_generator \
    --hidden-import apps.capability_adapter.litigation_generator \
    apps/workbench/scripts/slp_sidecar_entry.py
"""
import os
import sys

# PyInstaller onefile 模式下，项目根通过环境变量传入
project_root = os.environ.get("SLP_PROJECT_ROOT", "")
if project_root:
    sys.path.insert(0, project_root)

import uvicorn  # noqa: E402


def main():
    port = int(os.environ.get("SLP_SIDECAR_PORT", "8790"))
    from apps.capability_adapter.app import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
