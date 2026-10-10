# law-search 自检 Makefile（P2-3）
# 用法：
#   make test          # quick 自检：编译 + 全链 import + Guard/QU 断言（秒级）
#   make lint          # 全量语法编译检查
#   make ingest_check  # 知识库只读体检：chunk 数 / 快照 / hash 索引
#   make smoke         # 检索级自检：加载 Embedding+Reranker（~20s）
#   make e2e           # 端到端自检：含 LLM + Guard（~25s，需 Provider 可用）
#   make test_office   # Office 读取单元测试（doc/docx/pptx/xlsx/xls/错标重定向，秒级）
#   make l2-sync       # L2 官方源同步检查（Tavily ~9 次/轮，纯 CPU，不加载模型）
#   make l2-install    # 安装 launchd 周定时任务（每周一 08:30 自动 l2-sync）
#   make all           # test + lint + ingest_check + test_office

PY ?= /Users/simon/Documents/1-SLP/venv/bin/python

.PHONY: test lint ingest_check smoke e2e test_office l2-sync l2-install all

test:
	$(PY) tests/selfcheck.py --level quick

lint:
	$(PY) -m compileall -q core config server.py ingest.py

ingest_check:
	$(PY) tests/selfcheck.py --level ingest

smoke:
	$(PY) tests/selfcheck.py --level smoke

e2e:
	$(PY) tests/selfcheck.py --level e2e

test_office:
	$(PY) tests/test_office_reader.py

l2-sync:
	$(PY) sync_l2_official.py

l2-install:
	$(PY) sync_l2_official.py --install

all: test lint ingest_check test_office
