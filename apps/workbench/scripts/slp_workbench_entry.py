#!/usr/bin/env python3
"""SLP Workbench Backend - PyInstaller 入口点。

打包命令（项目根目录执行）:
  pyinstaller --name slp-workbench --onefile --paths . \
    --hidden-import apps.workbench.backend \
    --hidden-import apps.workbench.backend.app \
    apps/workbench/scripts/slp_workbench_entry.py
"""
import os
import sys

# PyInstaller onefile 模式下，项目根通过环境变量传入
project_root = os.environ.get("SLP_PROJECT_ROOT", "")
if project_root:
    sys.path.insert(0, project_root)

import uvicorn  # noqa: E402


def main():
    port = int(os.environ.get("SLP_WORKBENCH_PORT", "8780"))
    from apps.workbench.backend.app import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
