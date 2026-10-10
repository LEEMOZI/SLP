# DeepSeek Harness 架构审查：面向「一切皆插件」的未来就绪改造

> 审查基准：DeepSeek Harness 官方 extension-cookbook + architecture.md + packages/* 源码
> 审查版本：dsh 0.1+（Developer Preview）
> 审查日期：2026-08-30
> 代码基线：本项目 `/MCP/harness/` + `/dsh.config.json`

---

## 一、结论先行（30 秒版）

**❌ 当前项目只把 DSH 当成了"MCP Client + 启动外壳"——这是把 DSH 的功能用到了 1/5 不到，离"一切皆插件"的设计理念相差甚远。**

- 已接入：3 类（MCP 静态 server 注册 / Skill 卡片 JSON / Profile 分工 JSON）——**3/12**
- 未接入：9 类（LLM Adapter / Tool Plugin / Hook-Policy / Agent Loop / Sub-agent / Session Storage / Skill Provider / UI Plugin / Metrics）——**9/12**
- **P0 断裂**：dsh 官方 Profile 是**目录 + package.json + cordis.patch.yml** 的 bundle 组装，但我们的 profiles/*.json 是**任意命名的项目私有 JSON**——dsh 启动时根本读不到这些分工配置，这是必须在首次 `dsh web` 之前修的硬伤。

### 修复后预期效益

| 能力 | 当前实现（硬编码在 Python law-search 里） | 改造后（DSH 插件生态） |
|---|---|---|
| **新模型接入** | 改 Python `config/llm_config.py` + `core/llm_client.py` 双处 | 装一个 `llm-<vendor>` DSH 插件；0 代码 |
| **新工具接入** | 改 Python `server.py` + `pipeline.py` + registry 三处 | 装一个 `dsh-tool-<name>` 插件；0 代码 |
| **审批策略** | 无（模型调用什么工具由 Prompt 约束） | 装 `approval-policy` 插件；律师敏感操作（删库/写文件）强制人工确认 |
| **会话审计** | 仅 law-search logs（文本 line format） | 装 `audit-logger` 插件；全链路事件型日志，可导入 Elasticsearch |
| **模型路由** | 仅 Python 内 fallback 两层 | 装 `router-policy` 插件；支持按成本、延迟、能力多维路由，可热切换 |
| **Skill 动态注入** | 仅静态 `skills/*.json` 描述 | 装 `skill-provider-legal` 插件；按当前打开的案卷/案件类别自动加载对应司法解释 |
| **多工作流编排** | Python 端只有单 turn | 装 `subagent-fork` 插件；多代理并行（检索代理 / 文书代理 / 合同审查代理） |
| **UI 定制** | 无 UI 能力 | 装 `legal-ui` 插件；回答内嵌法条卡片 / 证据标记 / 证据链时间轴 |

---

## 二、Cordis 12 类扩展点 × 项目差距矩阵

| # | 扩展点 | DSH 接入方式 | 当前项目状态 | 差距描述 | 优先级 | 改造方向 |
|---|--------|------|--------|---------|--------|-----|
| 1 | **MCP Server Bridge** | `@deepseek-ai/dsh-mcp-client` + 每个 server 一个 Cordis 插件实例（dsh 自动为每个 mcp_registry 条目创建） | ✅ 已就绪（静态 JSON 提供 server） | law-search stdio 启动命令与 path 写死，无热加载 | **P1** | 把 mcp_registry 声明包进 P0 profile bundle（见 R3） |
| 2 | **LLM Adapter** | 写 `apply(ctx)` → `ctx.llm.registerAdapter(name, factory)` | ❌ 完全缺失 | **当前 LLM 路由硬编码在 law-search Python 代码里**，DSH 层毫无感知——这是 dsh 完全无法发挥作用的根因 | **P0** | P0 提供 `plugins/llm-router-bundle` 骨架 + 4 provider 插件化（见 R3） |
| 3 | **Tool Plugin（原生工具）** | `ctx.tools.register(defineTool({...}))` | ❌ 完全缺失 | 所有工具都走 MCP→law-search 的间接调用：7 个业务工具+OCR+Office 解析全是 RPC，带来 50-300ms/次 额外延迟 | **P1** | P1 为 OCR/Office Reader 写 DSH 原生工具插件（直接调 Python，跳过 MCP 串行） |
| 4 | **Hook / Policy（工具拦截）** | `ctx.on('tools/pre-execute')` 返回 `{kind: deny}`；`tools/execute` 包装执行；`tools/post-execute` 结果转换 | ❌ 完全缺失 | 现在"模型发什么调什么"，对敏感操作（`ingest_rebuild` 重建索引 / `review_contract` 读取保密目录 / shell 命令执行）没有审批闸 | **P0** | P0 提供 `plugins/policy-legal-safety`：审批策略骨架 + 白名单/黑名单 |
| 5 | **Agent Loop（思考-行动循环）** | `ctx.agent.loop = customStepFn` 可完全替换默认 Loop | ❌ 完全缺失 | DSH 默认 Loop 是"plan → tool → re-plan → done"——对于法律检索需要专用"法条引用核对"、"证据覆盖率检查"的额外步骤（正是我们 Python 里 answer_guard + citation 的流程），这部分全在 MCP 侧执行、对 DSH 黑盒 | **P2** | P2 提供 `plugins/agent-loop-legal`（目前先在 Python pipeline 里保留，等插件生态成熟后再迁移） |
| 6 | **Skill Provider** | 实现 `SkillProvider{list(), get()}` → `ctx.skills.registerProvider()` | ⚠️ 静态 JSON 卡片（非 Provider 注册） | 3 个技能只是卡片描述；dsh 不会按案卷动态激活"侵权纠纷→专利法技能组"，也不会在检索失败时自动加载新的 skill | **P1** | P1 提供 `plugins/skill-provider-legal`（文件 system → DSH skill provider 桥接） |
| 7 | **Sub-agent Delegation** | `ctx.subagents.register(name, provider)` + `dsh-tool-subagent` 暴露给模型 | ❌ 完全缺失 | 现在一次大请求（比如"检索+审查+起草起诉状"）所有工作由单 python server.py 串行处理；没有"检索子代理 / 合同审查子代理 / 文书起草子代理"的并行隔离执行 | **P1** | P1 提供 `plugins/subagent-legal-bundle`（fork-in-process 三种 worker 配置） |
| 8 | **Session / Storage** | 实现 `StorageBackend{append(), replay()}` → 替换 `ctx.sessions.storage` | ❌ 完全缺失 | 默认 session 是内存存储，重启即丢；法律工作需要会话保留、可追溯、可导出到案卷管理系统 | **P1** | P1 提供 `plugins/session-storage-fs`（结构化 JSONL 到 law-search/logs/） |
| 9 | **UI Plugin / Conversation Node** | 注册 `ConversationNodeDefinition` + 渲染器；或浏览器插件侧接入 | ❌ 完全缺失 | 当前只有纯文本输出；法律回答需要"法条原文折叠卡片 / 证据标记 / 引用跳转原文页"等富 UI | **P2** | P2 提供 `plugins/ui-legal-citations`（Conversation Node 骨架） |
| 10 | **Profile（组装层）** | `~/.dsh/profiles/<name>/` 目录 + `package.json`（dependencies 声明 bundle）+ `cordis.patch.yml`（增量覆盖） | ⚠️ 项目私有 JSON（无法被 dsh 加载） | **P0 最严重问题**：profiles/*.json 是我们自定的格式，dsh 不会读它——需要转成标准 profile + bundle | **P0** | P0 提供 `harness/dsh-home/profiles/lm-studio-workbench/{package.json,cordis.patch.yml}` + `work-project/` 双 profile |
| 11 | **Bundle（分发格式）** | 任意 npm 包，`package.json` 含 `"dsh": { "bundle": ["./dist", "./cordis.patch.yml"] }`，可 `dsh plugin add <bundle-name>` 一键安装 | ❌ 完全缺失 | 现在没有任何插件能被 `dsh plugin add` 安装；用户不能复用社区插件，自己写的也不能热部署 | **P0** | P0 提供 3 个 bundle 骨架（见下）：`law-search-dsh-bundle`、`policy-legal-safety-bundle`、`profiles-legal-bundle` |
| 12 | **Metrics / Observability** | `ctx.on('tools/post-execute'/'llm/*/stream-end'/'session/event')` 发指标到 OTLP / Prometheus / 控制台 | ❌ 完全缺失 | 现在 law-search 有简单 text log；没有 DSH 统一观测链：哪个工具耗时多少 / 哪个 provider 延迟高 / 哪个技能加载失败 / 成本追踪 | **P2** | P2 提供 `plugins/observability-bundle`（OTLP 兼容） |

### 合计

- **P0（首次启动前必需）**：2 项 + 3 个 Bundle 骨架 = **P0**
  - 10 Profile（**断裂**：dsh 启动读不到分工配置）
  - 4 Policy Hook（法律业务不能没有敏感操作闸）
  - 2 LLM Adapter（**断裂**：DSH 无法编排模型路由，全部压力落到 Python server）
  - 11 Bundle（打包才能一键安装/升级/发布）
- **P1（两周内完成，插件化提升效率显著）**：3, 6, 7, 8
- **P2（视社区插件成熟度推进）**：5, 9, 12

---

## 三、改造后的目录蓝图（DSH 就绪）

```
MCP/harness/
├── mcp_registry.json                # 现有（已修 $schema）。由 bundle 间接引用
├── model_router.json                # 现有（已修 $schema）。DSH LLM Router 插件从这里读配置
├── check_config_consistency.py      # 现有
├── install_mcp.sh                   # 现有（把注册表铺到 ~/.dsh/{servers,mcp,profiles,skills}）
│
│ ── 新增：DSH HOME 标准布局（配合 install_mcp.sh 铺到 ~/.dsh）───────────────────
├── dsh-home/                        # 镜像目标：~/.dsh
│   ├── cordis.patch.yml             # 全局补丁：启用 policy-legal-safety / observability / router
│   └── profiles/
│       ├── lm-studio-workbench/
│       │   ├── package.json         # 声明依赖 profiles-legal-bundle / policy-legal-safety-bundle / law-search-dsh-bundle
│       │   └── cordis.patch.yml     # 工作台级补丁：默认模型、禁用 Bionic channel、绑定技能组
│       └── work-project/
│           ├── package.json         # 同上 + bionic workbench 插件
│           └── cordis.patch.yml
│
│ ── 新增：DSH 插件包（我们自己开发的 Cordis 插件 & bundle）────────────────────
└── plugins/                         # 开发时工作区；npm 发布后可直接 dsh plugin add <name>
    │
    ├── law-search-dsh-bundle/       # 【Bundle 1：法律检索全家桶】一键安装=law-search MCP+路由+工具桥
    │   ├── package.json             # name:"@law-agent/law-search-dsh-bundle"，"dsh.bundle": [...]
    │   ├── cordis.patch.yml         # 声明加载 law-search mcp server；绑定 llm-router-bundle
    │   ├── README.md
    │   └── tsconfig.json            # （可选）后续写 TS 插件时用
    │
    ├── llm-router-bundle/           # 【Bundle 2：LLM 路由器插件化】
    │   ├── package.json
    │   ├── cordis.patch.yml
    │   └── src/
    │       └── router.ts            # ctx.llm.registerAdapter() × 4；读 model_router.json
    │
    ├── policy-legal-safety-bundle/  # 【Bundle 3：法律安全审批策略】
    │   ├── package.json
    │   ├── cordis.patch.yml
    │   └── src/
    │       └── safety.ts            # tools/pre-execute 拦截：ingest_rebuild/保密目录 → 审批；敏感词阻断
    │
    ├── skill-provider-legal/        # 【P1 Plugin】skills/*.json → DSH SkillProvider
    │   ├── package.json
    │   └── src/provider.ts
    │
    ├── session-storage-fs/          # 【P1 Plugin】结构化 JSONL 案卷存储
    │   ├── package.json
    │   └── src/storage.ts
    │
    ├── subagent-legal-bundle/       # 【P1 Bundle】检索/审查/起草 三子代理 fork
    │   ├── package.json
    │   └── cordis.patch.yml
    │
    ├── agent-loop-legal/            # 【P2 Plugin】法条核对 + 证据覆盖率 专用步骤
    │   └── package.json
    │
    ├── ui-legal-citations/          # 【P2 Plugin】法条卡片 UI
    │   └── package.json
    │
    ├── observability-bundle/        # 【P2 Bundle】成本/延迟/命中率 OTLP
    │   └── package.json
    │
    └── README.md                    # 插件开发指南：如何写一个插件 / 如何调试 / 如何发布
```

### 为什么要这样分

1. **Bundle = 插件 + 配置的集合**，dsh 官方的安装单位是 bundle，不是单个 JSON。我们 3 个 P0 bundle 可以做到"安装 `law-search-dsh-bundle`"即等于"连 MCP server + LLM 路由 + 审批策略 + 技能 + 会话存储"一次性全装好——这就是「一切皆插件」的实际体现：**能力叠加不是改代码，是装 bundle**。
2. **Profile 从"静态 JSON"变成"目录 + package.json + cordis.patch.yml"**：这是 dsh 官方唯一识别的格式。lm-studio-workbench 和 work-project 各自独立目录，互不干扰，可热切换。
3. **`dsh-home/` 作为 ~/.dsh 的模板**，用 install_mcp.sh 铺到用户目录，避免每次重装 dsh 后手工同步 7 份文件。

---

## 四、升级策略（对 law-search Python 侧 0 侵入 / 渐进迁移）

核心目标：**不改变 law-search 业务代码，在 DSH 层叠加能力**。逐步迁移顺序：

### Phase 1（P0：首次启动前，本次落地）
```
  ┌──────────────────────────────────────────────────┐
  │                DeepSeek Harness                   │
  │  Profile: lm-studio-workbench                     │
  │  ┌──────────────┐   ┌──────────────────────┐      │
  │  │ policy-legal │   │ llm-router           │      │
  │  │ -safety      │   │ - (lm_studio → DS)   │      │
  │  └──────┬───────┘   └──────────┬───────────┘      │
  │         │ 审批闸              ┌┴┐                  │
  │         │ tool 调用            │ LLM 调用           │
  │  ┌──────┴───────┐    ┌─────────▼──────────┐       │
  │  │ law-search   │    │ (provider selection)│       │
  │  │ MCP server   │    │  Python llm_client   │       │
  │  └──────────────┘    └────────────────────┘       │
  └──────────────────────────────────────────────────┘
```
Phase 1 的 LLM 调用仍走 Python llm_client（不改代码），DSH llm-router 只做策略选择提示，避免破坏已验证的链路。

### Phase 2（P1：两周内）
- 技能 Provider / Session 存储 / Sub-agent / OCR 原生工具 四个插件上线
- 合同审查走「contract-review sub-agent」独立上下文，证据收集与比对并行

### Phase 3（P2：插件生态成熟后）
- Agent Loop 迁移：法条引用核对 / 证据覆盖率检查 从 Python answer_guard 迁入 DSH Loop
- UI 插件：法律引用跳转 / 证据链时间轴
- OTLP 指标接入，可观察「哪个工具耗时多少 / 每次调用成本」

---

## 五、DSH 插件生态如何带来「持续成长」（回答你的核心问题）

| 需求场景 | 不用插件体系的做法 | 接入插件后的做法 |
|---|---|---|
| 接入新的本地推理（如 Ollama / vLLM） | 改 `config/llm_config.py` + `core/llm_client.py` 两处；需要新增 Provider 类 + 单元测试 | `dsh plugin add @deepseek-ai/dsh-llm-ollama`；模型列表自动刷新；0 代码 |
| 接入新的 LLM（如 GPT-5 / 月之暗面 / 智谱） | 同上 | `dsh plugin add dsh-llm-moonshot`；0 代码 |
| 新增工具（如工商查询 / 法院公告 / 裁判文书在线查询） | 写 MCP server → 改 registry → 测试；需要 Python 开发整个服务器 | 找个社区 `dsh-tool-wenshu` 插件；`dsh plugin add`；0 代码就能给模型提供裁判文书检索能力 |
| 安全合规（某客户要求所有外部 API 调用需审批） | 改 python server.py 加审批 hook，多客户多份分支 | 装 `approval-policy` 插件；配置 YAML + 自定义审批流；law-search 零修改 |
| 换一种 Agent 策略（比如 Tree-of-Thoughts 对复杂案件推理） | 改 `pipeline.py` 重写主循环，高风险 | 装 `agent-loop-tot` 插件；启动参数切 `--agent-preset tot`；旧循环仍可回退 |
| 升级日志系统（从本地文件→Elasticsearch） | 改 Python logger + 新写 ES sink | 装 `session-storage-es` 插件；历史日志自动迁移 |
| UI 定制（需要证据链时间轴 / 案件日历） | 写独立 Web 前端，对接 law-search API | 装 `ui-timeline-case` 浏览器插件；和官方 Web UI 原生融合 |

**关键差异**：不用插件体系 = 每次能力扩展都是「改 Python 代码」，版本管理困难、每次变更都要回滚测试；接入插件体系 = 每次能力扩展都是「安装一个 npm 包」，热插拔、可回滚、全社区共享。

---

## 六、P0 代码级交付清单（本次立即落地）

1. ✅ `harness/dsh-home/cordis.patch.yml` — 全局补丁（引用 P0 三 bundle）
2. ✅ `harness/dsh-home/profiles/{lm-studio-workbench,work-project}/{package.json,cordis.patch.yml}` × 4 个文件
3. ✅ `harness/plugins/law-search-dsh-bundle/{package.json,cordis.patch.yml,README.md}`
4. ✅ `harness/plugins/llm-router-bundle/{package.json,cordis.patch.yml,README.md}`
5. ✅ `harness/plugins/policy-legal-safety-bundle/{package.json,cordis.patch.yml,README.md,src/safety.ts 骨架}`
6. ✅ `harness/plugins/README.md` — 插件开发指引（如何本地 link 调试 / 如何安装 / 如何发布）
7. ✅ `harness/install_mcp.sh` 升级：额外铺设 dsh-home/* → ~/.dsh/（覆盖 profile / cordis.patch）
8. ✅ `dsh.config.json` 升级：指向标准 profile 路径（原 project-level profile / skill → profile 包 引用）
