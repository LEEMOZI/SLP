# 法律智能检索系统 — 项目状态

> 更新时间：2026-10-12（judgment-search 全量建库 + 高频词调优 44x + 三服务全在线 + MCP 全客户端接入 + session-storage 服务端化 + 交付链路上线）

## 1. 目标

本地化中国法律检索与分析系统，通过 MCP 协议对外暴露法律检索、裁判文书检索、合同审查、文书生成、结果交付等能力。

**工作原则**：LM Studio 作为模型管理后台（开机自启，常驻 127.0.0.1:1234 API 服务），DSH Desktop 为主工作界面。本地模型承担主要生成任务，云端模型只负责复杂任务与超长上下文。

## 2. 架构

```
本地客户端（MAC Studio）              远程客户端（MacBook，Tailscale 100.66.217.64）
LM Studio / DSH Desktop               DSH Desktop（瘦客户端）/ Cherry Studio / WorkBuddy
        │  stdio / http                          │  streamable-http + Bearer Token
        └────────────┬───────────────────────────┘
                     ▼
  ┌─────────────────────────────────────────────────────┐
  │ law-search MCP（端口 8765，12 个工具）                 │
  │ ├─ search_law / search_case / search_contract /      │
  │ │  search_reference                                  │
  │ ├─ chroma_status / ingest_rebuild                    │
  │ ├─ write_litigation / review_contract /              │
  │ │  evaluate_evidence                                 │
  │ ├─ read_document                                     │
  │ └─ export_result / deliver_file（交付链路）           │
  │    └─ /files/<filename> 静态下载路由                  │
  ├─────────────────────────────────────────────────────┤
  │ judgment-search MCP（端口 8766）                      │
  │ └─ 6862 万裁判文书全文检索（按年分库 FTS5）            │
  ├─────────────────────────────────────────────────────┤
  │ session-storage-cloud MCP（端口 8767）                │
  │ └─ 多设备共享会话记忆（SQLite+FTS5，7 工具）           │
  └─────────────────────────────────────────────────────┘
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
    ChromaDB    LLM Router    Office/OCR
   (向量检索)  (本地→云端)   (文档解析)

服务部署：launchd 接管（RunAtLoad + KeepAlive，崩溃自动拉起）
端口 0.0.0.0 监听，禁止公网暴露，仅 Tailscale / SSH 隧道访问
交付输出：~/SLP交付输出/（自动同步 iCloud Drive）
```

### 模型路由（auto 模式）

| 任务 | Provider | 模型 |
|---|---|---|
| 短任务（< 8000 字符） | lm_studio_local | qwen/qwen3.8-27b（gguf, 32k ctx） |
| 长任务（≥ 8000 字符） | deepseek_api | deepseek-chat（384K 上限） |
| 本地失败 fallback | deepseek_api | 自动切换 |
| MCP 内部生成 | lm_studio_local | MAC Studio 27B 本地优先，失败回落 DeepSeek 云端（省费用） |
| Bionic / LM Link | 预留 | 待激活 |

## 3. 目录结构

```
1-SLP/
├── MCP/
│   ├── law-search/         # MCP 服务核心（端口 8765）
│   │   ├── server.py       # MCP 入口（stdio + streamable-http 双模式，12 个工具）
│   │   ├── ingest.py       # 知识库构建（增量 + OCR 子进程隔离）
│   │   ├── config/         # 统一配置包（paths.yaml 四库 NAS 路径 + llm_config.py）
│   │   ├── core/           # 业务逻辑层（含 ocr_service.py dpi=150 修复）
│   │   ├── tests/          # 测试套件（office_reader / selfcheck / version_spot_check）
│   │   ├── chroma/         # 向量数据库持久化（4 collection）
│   │   └── chroma_snapshots/  # 快照（循环保留）
│   ├── judgment-search/    # 裁判文书检索 MCP（端口 8766，2026-10-12 上线）
│   │   ├── build_judgment_db.py  # 建库脚本（按年分库，B' 架构）
│   │   ├── server.py       # MCP 入口（search / get_judgment / health / filter_years）
│   │   ├── check_build.py  # 建库巡检
│   │   └── data/judgments/ # 37 个分年 SQLite 库（426.5 GB）
│   ├── session-storage/    # 会话记忆 MCP（本地 stdio + 云端 8767 双形态）
│   │   ├── server.py       # 本地 stdio 入口
│   │   ├── cloud_server.py # streamable-http 服务端（2026-10-12 新增）
│   │   └── com.session-storage.cloud.plist
│   ├── harness/            # DSH 插件 + 配置
│   │   ├── plugins/        # 10 个 bundle（3 P0 + 3 P1 + 3 P2 + session-storage-cloud-sync）
│   │   ├── skills/         # 技能定义
│   │   ├── mcp_registry.json
│   │   └── install_mcp.sh
│   └── venv/               # Python 虚拟环境
├── dsh.config.json
└── PROJECT_STATUS.md
```

## 4. 知识库现状

### 4.1 向量库（ChromaDB，2026-10-11 全量重建完成）

| 库名 | chunks | 数据源（NAS 挂载卷） | 状态 |
|---|---|---|---|
| law_library | 268,969 | 1-2 法律法规 | ✅ 2026-10-11 08:32 |
| case_library | 1,033 | 1-8 知识库/参考判例 | ✅ 2026-10-11 08:33 |
| contract_library | 37,736 | 1-8 知识库/文书模板 | ✅ 2026-10-11 08:51 |
| reference_library | 170,839 | 1-8 知识库/业务指南 | ✅ 2026-10-11 11:23 |
| **合计** | **478,577** | 四库快照齐整 | 检索稳态 0.18s，冒烟通过 |

- 数据源统一为 NAS 路径，MacBook 与 NAS 双向实时同步，MAC Studio 跑 ingest 即可增量入库
- OCR 段错误修复：dpi 150 + 子进程隔离 + onnxruntime 固定 1.19.2
- 遗留 80 个问题文件（11 加密 PDF + 69 旧版 .ppt），见 `problem_files_20261011.md`
- 基准测试（2026-10-11）：Phase A Hit@1 100%，Phase B 答案关键词覆盖 96%，Guard 调优后 pass_rate 100%

### 4.2 裁判文书库（judgment-search，2026-10-12 全量建成）

| 指标 | 数值 |
|---|---|
| 年份覆盖 | 1985–2021（37 年连续，无缺口） |
| 文档总数 | **68,627,941**（/health 与各库 MAX(id) 一致） |
| 磁盘占用 | 426.5 GB（`MCP/judgment-search/data/judgments/`） |
| 架构 | B'：按年分库 SQLite + docs 表 zstd 压缩全文 + contentless FTS5 倒排 |
| 中文检索 | 入库前 CJK 逐字空格化（seg）+ 查询侧同款转换 + 短语匹配，支持任意长度子串 |
| 高频保护 | 两档简化 + EXISTS 探针 + 排序取数两阶段分离（2026-10-12 调优） |

抽样验收（调优后，本地 Python 直接调用）：
- 高频词达标：盗窃罪 13.3s→0.3s（44x）、民间借贷 利息 16.8s→0.7s（24x）、合同纠纷 4.6s→0.1s（46x）
- 基准查询：毛善亮 0.7s、盗窃罪+court过滤 0.3s、get_judgment 3ms
- 优化原理：探针用 EXISTS OFFSET 20000（双词 AND FTS 交集求扫 20 万行慢 7-8x，去掉中间档直接入库序兜底）；排序阶段纯 FTS 表无 JOIN 无 BLOB，大字段延迟到 Phase 2 IN 查询才读取

### 4.3 会话记忆库（session-storage-cloud，2026-10-12 服务端化）

- 端口 8767，streamable-http + Bearer Token，SQLite+FTS5 本地存储
- 多设备共享记忆（MacBook / MAC Studio 经 Tailscale 统一访问），7 个工具全部可用
- 消除此前单机版「多设备记忆孤岛」问题

## 5. 完成度

| 模块 | 进度 | 说明 |
|---|---|---|
| law-search MCP | 100% | 12 个工具（2026-10-12 新增 export_result / deliver_file）；launchd 接管 |
| judgment-search MCP | 100% | 37 年 6862 万文书全量建库；高频词调优后盗窃罪 0.3s/民间借贷 利息 0.7s（5s 内）；launchd 接管 |
| session-storage 服务端化 | 100% | cloud_server.py + launchd + 两台 DSH 接入（2026-10-12） |
| 交付链路 | 100% | ~/SLP交付输出/ + iCloud 同步 + /files/ 静态路由 + Tailscale URL；任意格式原样交付 |
| 工具收敛架构 | 100% | 三层同向防护，实测 1 次调用出完整答案 |
| LLM 多 Provider 路由 | 100% | 4 槽位，auto 路由+回退链；内部生成本地优先 |
| 知识库构建 | 100% | 四库 478,577 chunks 全量重建，增量入库已验证（每日 03:30 launchd） |
| DSH 插件架构 | 100% | 3 P0 + 3 P1 + 3 P2 + cloud-sync，扩展点全部接入 |
| MCP 客户端接入 | 100% | 两台 DSH 三层配置对齐（17 servers）：law-search-cloud / judgment-search / session-storage-cloud / 9×pkulaw / wk-mcp / chinese-law / filesystem |
| pkulaw 全系列 | 100% | 9 个服务全部迁移 apim-gateway + streamable-http（旧 SSE 已清零） |
| 答案守卫 | 100% | 法条号数字归一化调优后 pass_rate 100%，硬警告一票否决 |
| LM Studio 本地模型 | 95% | qwen3.8-27b（32k ctx，支持最大 256K，可按需加大） |
| DeepSeek API | 80% | 密钥文件回退已配置，auto 路由已生效 |
| Bionic / LM Link | 10% | Provider 槽位预留，待激活 |
| MacBook 瘦客户端 | 100% | SSH 免密 + Tailscale 组网 + 全部 MCP 远程调用 Studio |

## 6. 待办优先级

### 已完成里程碑（2026-09 ~ 2026-10）

- [x] LLM 多 Provider 路由重构 + Model ID 三方统一
- [x] Office 文件解析 + OCR dpi 隔离修复
- [x] 四库全量入库 478,577 chunks + 增量链路验证
- [x] DSH 插件架构 P0/P1/P2 全量补齐（9 bundle）
- [x] law-search-cloud 上线（streamable-http + Bearer Token，SSE 已清零）
- [x] 远程访问三设备组网（Tailscale + SSH 免密）
- [x] Guard 调优（法条号归一化，pass_rate 0.6→1.0）
- [x] 基准测试（Phase A Hit@1 100%，Phase B 覆盖 96%）
- [x] deliver_file / export_result + /files/ 交付链路（2026-10-12）
- [x] judgment-search 全量建库 37 年 6862 万份 + 三档排序保护（2026-10-12，git 9393e1c）
- [x] session-storage 服务端化 + 两台 DSH MCP 配置全量对齐（2026-10-12）
- [x] judgment-search 高频词排序调优（2026-10-12，git 0309172）：两档简化 + EXISTS OFFSET 20000 探针 + 排序取数两阶段分离；盗窃罪 13.3s→0.3s（44x）、民间借贷 利息 16.8s→0.7s（24x）、合同纠纷 4.6s→0.1s（46x）
- [x] 自动化运维：launchd 自启 ×3 + 增量 ingest 每日 03:30 + auto_benchmark.sh + auto_sync_kb.sh

### 当前待办（2026-10-12 审计产出）

- [ ] 80 个问题文件人工处理（11 加密 PDF 可尝试 qpdf 修复；69 旧版 .ppt 需另存为 .pptx）
- [ ] zstd 全文 blob 拆分迁移决策：建库已完成，可评估将 ~190GB 全文 blob 迁移 NAS 只读挂载，FTS+元数据留本地（仅全文查看依赖 NAS 在线）
- [ ] 激活 Bionic / LM Link Provider
- [ ] lm-studio-workbench profile 完成 pnpm install
- [ ] wk-mcp 令牌验证（记忆中旧令牌返回 401，需重新获取并同步两台 DSH servers 配置）

### 已知问题（不阻塞使用）

- Chroma 大集合精确计数触发 SQLite "too many SQL variables"（外观性问题，检索不受影响）
- law-search 服务日志不含客户端 IP 字段，溯源只能靠时间戳对齐
- DSH 配置修改后必须 Cmd+Q 完全退出重启才生效；客户端 MCP 进程长期存活，代码更新需重启客户端

## 7. 关键配置文件

| 用途 | 路径 |
|---|---|
| law-search 服务入口 | [MCP/law-search/server.py](MCP/law-search/server.py) |
| 知识库构建 | [MCP/law-search/ingest.py](MCP/law-search/ingest.py) |
| judgment-search 服务入口 | [MCP/judgment-search/server.py](MCP/judgment-search/server.py) |
| judgment-search 建库脚本 | [MCP/judgment-search/build_judgment_db.py](MCP/judgment-search/build_judgment_db.py) |
| session-storage 云端入口 | [MCP/session-storage/cloud_server.py](MCP/session-storage/cloud_server.py) |
| LLM 路由配置 | [MCP/law-search/config/llm_config.py](MCP/law-search/config/llm_config.py) |
| launchd 服务（×4） | `~/Library/LaunchAgents/com.{law-agent.cloud, law-agent.ingest, judgment-search.cloud, session-storage.cloud}.plist` |
| DSH Desktop 配置 | `~/Library/Application Support/dsh-desktop/harness/` |
| DSH Web UI 配置 | `~/.dsh/` |
| LM Studio mcp.json | `~/.cache/lm-studio/mcp.json` |
| 交付输出目录 | `~/SLP交付输出/`（同步 iCloud Drive） |
| 项目根配置 | [dsh.config.json](dsh.config.json) |
| MCP 注册表 | [MCP/harness/mcp_registry.json](MCP/harness/mcp_registry.json) |
| 插件安装脚本 | [MCP/harness/install_mcp.sh](MCP/harness/install_mcp.sh) |

## 8. 运维速查

```bash
# 重启服务（launchctl load 在新版 macOS 已废弃，用 kickstart）
launchctl kickstart -k gui/501/com.law-agent.cloud
launchctl kickstart -k gui/501/com.judgment-search.cloud
launchctl kickstart -k gui/501/com.session-storage.cloud

# 健康检查（本机 / Tailscale）
curl http://127.0.0.1:8765/health   # law-search
curl http://127.0.0.1:8766/health   # judgment-search
curl http://127.0.0.1:8767/health   # session-storage
curl http://100.66.217.64:8766/health  # 跨设备验证

# 增量入库（每日 03:30 自动执行，也可手动触发）
launchctl kickstart -k gui/501/com.law-agent.ingest
```
