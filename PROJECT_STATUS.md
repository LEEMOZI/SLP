# 法律智能检索系统 — 项目状态

> 更新时间：2026-10-11（全量重建完成 + OCR 段错误修复 + launchd 接管）

## 1. 目标

本地化中国法律检索与分析系统，通过 MCP 协议对外暴露法律检索、合同审查、文书生成等能力。

**工作原则**（2026-09-01 更新）：当前 8GB 内存机器上，LM Studio 作为主界面；新主机到货后，LM Studio 退居模型管理后台（开机自启，常驻 127.0.0.1:1234 API 服务），DSH Desktop 升级为主工作界面。本地模型承担主要生成任务，云端模型只负责复杂任务。

## 2. 架构

```
本地客户端（MAC Studio）              远程客户端（MacBook 等，Tailscale 100.66.217.64）
LM Studio / Bionic / DSH Desktop      Cherry Studio / WorkBuddy / DSH Desktop
        │  stdio                                  │  streamable-http + Bearer Token
        └────────────┬────────────────────────────┘
                     ▼
            law-search MCP Server（10 个工具，含 search_reference）
            ├─ search_law / search_case / search_contract / search_reference
            ├─ chroma_status / ingest_rebuild
            ├─ write_litigation / review_contract / evaluate_evidence
            └─ read_document
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
    ChromaDB    LLM Router    Office/OCR
   (向量检索)  (本地→云端)   (文档解析)

服务部署：launchd 接管（com.law-agent.cloud，RunAtLoad + KeepAlive）
端口 0.0.0.0:8765，禁止公网暴露，仅 Tailscale / SSH 隧道访问
```

### 模型路由（auto 模式）

| 任务 | Provider | 模型 |
|---|---|---|
| 短任务（< 8000 字符） | lm_studio_local | qwen/qwen3.8-27b（gguf, 32k ctx） |
| 长任务（≥ 8000 字符） | deepseek_api | deepseek-chat |
| 本地失败 fallback | deepseek_api | 自动切换 |
| **MCP 内部生成**（search_law 等，2026-10-09 起） | lm_studio_local | MAC Studio 27B 本地模型优先，失败回落 DeepSeek 云端（省费用） |
| Bionic / LM Link | 预留 | 待激活 |

## 3. 目录结构

```
1-SLP/                     # 项目已从 0-AI 迁移并更名（2026-10，现全部在 MAC Studio）
├── MCP/
│   ├── law-search/         # MCP 服务核心
│   │   ├── server.py       # MCP 入口（stdio + streamable-http 双模式，10 个工具）
│   │   ├── ingest.py       # 知识库构建（增量 + OCR 子进程隔离）
│   │   ├── config/         # 统一配置包（paths.yaml 四库 NAS 路径 + llm_config.py）
│   │   ├── core/           # 业务逻辑层（含 ocr_service.py dpi=150 修复）
│   │   ├── tests/          # 测试套件（office_reader / selfcheck / version_spot_check）
│   │   ├── chroma/         # 向量数据库持久化（4 collection）
│   │   ├── chroma_snapshots/  # 快照（循环保留，2026-10-11 四库齐整）
│   │   └── requirements.txt
│   ├── harness/            # DSH 插件 + 配置
│   │   ├── plugins/        # 9 个 bundle（3 P0 + 3 P1 + 3 P2）
│   │   ├── skills/         # 技能定义
│   │   ├── mcp_registry.json  # MCP 注册表（9 服务）
│   │   └── install_mcp.sh  # 安装脚本
│   └── session-storage/    # 会话记忆独立 MCP（SQLite+FTS5）
├── venv/                    # Python 虚拟环境
├── dsh.config.json          # 项目根配置
└── PROJECT_STATUS.md
```

## 4. 知识库现状（2026-10-11 全量重建完成）

| 库名 | chunks | 数据源（NAS 挂载卷） | 状态 |
|---|---|---|---|
| law_library | 268,969 | 1-2 法律法规 | ✅ 2026-10-11 08:32 |
| case_library | 1,033 | 1-8 知识库/参考判例 | ✅ 2026-10-11 08:33 |
| contract_library | 37,736 | 1-8 知识库/文书模板 | ✅ 2026-10-11 08:51 |
| reference_library | 170,839 | 1-8 知识库/业务指南 | ✅ 2026-10-11 11:23（新建库首次全量） |
| **合计** | **478,577** | 四库快照齐整（chroma_snapshots/） | 检索冒烟通过（稳态 0.18s） |

- 数据源统一为 NAS 路径：`/Volumes/sata11-138XXXX2122/文档同步/电脑备份/MacBook Pro/1-Work/...`
- MacBook 与 NAS 双向实时同步，MAC Studio 跑 ingest 即可增量入库
- OCR 段错误修复（2026-10-11）：dpi 200→150（质量实测无损）+ 子进程隔离（单文件失败仅跳过）+ onnxruntime 固定 1.19.2
- 遗留 80 个问题文件：11 个损坏/加密 PDF（OCR 隔离层跳过）+ 69 个旧版二进制 .ppt（textutil 不支持）
- 裁判文书全量数据（NAS 94GB）规划为独立 judgment-search（SQLite FTS5 原位索引，不走 paths.yaml），未动工

## 5. 完成度

| 模块 | 进度 | 说明 |
|---|---|---|
| MCP 服务 | 100% | 10 个工具（含 search_reference，2026-10-10）；launchd 接管自启（2026-10-11） |
| 工具收敛架构 | 100% | 三层同向防护（patch 铁律 + docstring 禁令 + STOP SIGNAL），实测 1 次调用出完整答案 |
| LLM 多 Provider 路由 | 100% | 4 槽位，auto 路由+回退链；内部生成本地优先（2026-10-09） |
| 知识库构建 | 100% | 四库 478,577 chunks 全量重建（2026-10-11），增量入库已验证 |
| **DSH 插件架构** | **100%** | **P0 + P1 + P2 全部补齐（2026-09-03）：3 P0 + 3 P1 + 3 P2 = 9 个 bundle，12 类扩展点全部接入** |
| session-storage 插件 | 90% | 独立 Python MCP（6 工具），SQLite+FTS5；LM Studio 已接入，DSH/其他客户端待接入 |
| LM Studio 本地模型 | 95% | qwen/qwen3.8-27b（MAC Studio 2026-10-09），32k ctx；备选 gemma-4-31b-qat / bonsai-27b / muse-glimmer |
| DeepSeek API | 80% | 密钥文件回退已配置，auto 路由已生效 |
| Bionic / LM Link | 10% | Provider 槽位预留，待激活 |
| 远程访问 | 100% | Tailscale 三设备组网（2026-10-10）：MacBook / MAC Studio / iPhone；law-search-cloud 全客户端可达 |
| 答案守卫 | 100% | 语义覆盖率（3 阶段）+诚实拒绝豁免，已接入主流程 |
| Office 文件解析 | 100% | .doc/.docx/.pptx/.xlsx/.xls + 大文件截断；旧版二进制 .ppt 不支持（69 个待人工转换） |
| OCR | 100% | dpi=150 + 子进程隔离修复段错误（2026-10-11）；500 页/文件；11 个损坏 PDF 待人工处理 |

## 6. 待办优先级

### P0（已完成）
- [x] LLM 多 Provider 路由重构
- [x] DSH Desktop 安装 + 配置迁移
- [x] Model ID 三方统一（qwen3.5-4b）
- [x] Office 文件解析 + 单元测试
- [x] law_library 全量入库 376,718 chunks（2026-09-02）
- [x] 工具收敛架构三层防护（2026-09-02 验收：search_law 1 次调用出完整答案）
- [x] P1 插件 skill-provider / session-storage（session-storage 已同步接入 LM Studio mcp.json）
- [x] search_law 内部生成提速：固定路由云端 DeepSeek API（替代本地 JIT ~3min）
- [x] **DSH P0 插件架构补齐（2026-09-02）**：
  - [x] policy-legal-safety-bundle: src/safety.js（455行 CommonJS）实现 3 层拦截（工具分类 + 路径越权 + 高危关键词）
  - [x] llm-router-bundle: cordis.patch.yml 声明 4 个 Provider 配置（DeepSeek + LM Studio + 2个预留）
  - [x] law-search-dsh-bundle: cordis.patch.yml 完整声明 10 个 MCP server 挂载
  - [x] Profile 格式标准化：dsh-home 标准布局（lm-studio-workbench + work-project）
  - [x] 两个 Profile 的 cordis.patch.yml 声明安全策略（lm-studio-workbench: confirm / work-project: deny）
  - [x] check_config_consistency.py 升级为 v2（适配 dsh-home 标准布局）

### P1（能力补齐）
- [x] **DSH P1 插件架构补齐（2026-09-03）**：
  - [x] skill-provider-legal: src/provider.js（439行 CommonJS）实现 SkillProvider 接口（扫描 skillsDir → 格式校验 → ctx.skills.registerProvider 动态注册）
  - [x] subagent-legal-bundle: src/subagents.js（438行 CommonJS）实现 3 子代理注册（检索/审查/起草）+ 3 种并行策略
  - [x] Profile package.json 声明新增 P1 bundle（两个 Profile 均已更新）
  - [x] Profile cordis.patch.yml 启用 skill-provider-legal + subagent-legal 配置
  - [x] install_mcp.sh 升级支持 P1 插件符号链接
  - [x] check_config_consistency.py 升级为 v3（新增 P1 bundle 校验）
  - [x] session-storage-fs: src/storage.js（465行 CommonJS）实现 StorageBackend 接口（append/replay）+ 会话管理器（search/get/tag/export/stats/sync）+ 自动同步定时器；通过 child_process 桥接 Python session-storage MCP 的 SQLite+FTS5 后端（2026-09-03）
- [x] 刑法/劳动法/婚姻法语料入库（2026-10-11 随四库全量重建完成）
- [x] 处置 4 个空 collection（2026-10-10 重构为 law/case/contract/reference 四库）
- [ ] 激活 Bionic / LM Link Provider
- [ ] lm-studio-workbench profile 完成 pnpm install

### P2（体验与规模）
- [x] **DSH P2 插件架构补齐（2026-09-03）**：
  - [x] agent-loop-legal: src/loop.js（413行 CommonJS）实现法律专用 Agent Loop（法条引用提取+核对+证据覆盖率检查+3种预设）
  - [x] ui-legal-citations: src/ui.js（444行 CommonJS）实现 4 个 UI 组件（法条折叠卡片+证据标记+引用跳转+证据时间轴）
  - [x] observability-bundle: src/metrics.js（465行 CommonJS）实现 3 个指标收集器+环形缓冲区+3种导出目标+成本计算
  - [x] Profile package.json 声明新增 P2 bundle（两个 Profile 均已更新）
  - [x] install_mcp.sh 升级支持 P2 插件符号链接
  - [x] check_config_consistency.py 升级为 v4（新增 P2 bundle 校验）
- [x] **部署 law-search-cloud（SSE + Bearer Token 鉴权）（2026-09-03）**：
  - [x] server.py 新增 `--auth-token` / `--auth-token-file` 参数，支持固定 token 或 `auto` 随机生成
  - [x] SSE transport 通过 `http_app()` + uvicorn 直接运行，中间件正确拦截未鉴权请求
  - [x] 创建 `run_cloud.sh` 启动脚本（支持 --transport / --host / --port / --token / --no-auth）
  - [x] 鉴权测试通过：无 token → 401 / 错误 token → 401 / 正确 token → 200
- [x] **远程访问部署（SSH 隧道 + Tailscale）（2026-09-03）**：
  - [x] setup_host.sh 一键部署：SSH 开启 + 防火墙配置 + Tailscale 安装 + launchd 自启动 law-search-cloud
  - [x] connect_remote.sh 升级：新增 law-search-cloud 端口转发（8765）
  - [x] REMOTE_ACCESS_GUIDE.md 更新：新增 law-search-cloud 远程接入配置 + token 读取方式
  - [x] launchd plist 模板：com.law-agent.cloud.plist 自启动 SSE + 鉴权
- [x] **自动化运维工具链（2026-09-03）**：
  - [x] 端到端集成测试：tests/integration_test.js（54 项检查：9 bundle 目录/文件/接口/Profile 声明/符号链接）
  - [x] 知识库增量维护：auto_sync_kb.sh（监控数据源变更 → 自动触发 ingest → 更新状态文件）
  - [x] 基准测试自动化：auto_benchmark.sh（Phase A/B 跑分 → 历史对比 → Markdown 报告）
- [ ] 语料补充后重测基准（目标 Hit@1 90%+）
- [x] 新主机到货部署完成（MAC Studio：LM Studio 27B + Tailscale 组网 + launchd 自启，2026-10）

## 6.1 当前待办（2026-10-11 审计产出）

- [ ] judgment-search 方案 B 开发：NAS 裁判文书 94GB，SQLite FTS5 原位索引，独立 MCP 服务（最大功能缺口）
- [ ] DSH Desktop 实机加载验证（配置侧已激活，需 Cmd+Q 重启后确认工具面板）
- [ ] session-storage 接入 DSH Desktop
- [ ] 增量 ingest launchd 排程（NAS 同步后夜间自动增量入库）
- [ ] 重建后基准重测（run_benchmark.py，建立 478k chunks 新基线）
- [ ] 80 个问题文件人工处理（11 损坏 PDF 可尝试 qpdf 修复；69 旧版 .ppt 需 PowerPoint/WPS 另存为 .pptx）

## 7. 关键配置文件

| 用途 | 路径 |
|---|---|
| MCP 服务入口 | [MCP/law-search/server.py](MCP/law-search/server.py) |
| 知识库构建 | [MCP/law-search/ingest.py](MCP/law-search/ingest.py) |
| LLM 路由配置 | [MCP/law-search/config/llm_config.py](MCP/law-search/config/llm_config.py) |
| DSH Desktop 配置 | `~/Library/Application Support/dsh-desktop/harness/` |
| DSH Web UI 配置 | `~/.dsh/` |
| LM Studio mcp.json | `~/.cache/lm-studio/mcp.json`（由 `~/.lmstudio-home-pointer` 指向） |
| 项目根配置 | [dsh.config.json](dsh.config.json) |
| MCP 注册表 | [MCP/harness/mcp_registry.json](MCP/harness/mcp_registry.json) |
| 插件安装脚本 | [MCP/harness/install_mcp.sh](MCP/harness/install_mcp.sh) |
