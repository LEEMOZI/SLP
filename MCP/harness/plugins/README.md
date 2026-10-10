# DSH 插件区 · 开发指引

> 目录：`MCP/harness/plugins/`
> 目标：把项目里的法律检索能力做成 DeepSeek Harness 的 Cordis 插件 / Bundle，让未来系统升级通过「安装新插件」而非「改 Python 代码」完成，实现"一切皆插件"。

---

## 一、当前插件清单（P0 + P1 + P2 全部补齐 ✅）

| 优先级 | 包名 | 目录 | 状态 | 功能 |
|---|---|---|---|---|
| **P0** | `@law-agent/law-search-dsh-bundle` | [law-search-dsh-bundle/](./law-search-dsh-bundle) | ✅ **完整** | MCP server 注册（9个工具 + chinese-law + pkulaw + wk-mcp） |
| **P0** | `@law-agent/llm-router-bundle` | [llm-router-bundle/](./llm-router-bundle) | ✅ **完整** | 4 Provider 配置声明（DeepSeek + LM Studio + 2个预留） |
| **P0** | `@law-agent/policy-legal-safety-bundle` | [policy-legal-safety-bundle/](./policy-legal-safety-bundle) | ✅ **完整** | 3层拦截：工具分类 + 路径越权 + 高危关键词 |
| **P1** | `@law-agent/skill-provider-legal` | [skill-provider-legal/](./skill-provider-legal) | ✅ **完整** | skills/*.json → `ctx.skills.registerProvider()` 动态激活 |
| **P1** | `@law-agent/subagent-legal-bundle` | [subagent-legal-bundle/](./subagent-legal-bundle) | ✅ **完整** | 检索/审查/起草 三子代理 fork 并行 |
| **P2** | `@law-agent/agent-loop-legal` | [agent-loop-legal/](./agent-loop-legal) | ✅ **完整** | 法条核对 + 证据覆盖率 专用 Agent Loop |
| **P2** | `@law-agent/ui-legal-citations` | [ui-legal-citations/](./ui-legal-citations) | ✅ **完整** | 法条卡片 / 证据时间轴 Conversation Node |
| **P2** | `@law-agent/observability-bundle` | [observability-bundle/](./observability-bundle) | ✅ **完整** | 成本/延迟/命中率 OTLP 指标 |
| P1 | `@law-agent/session-storage-fs` | [session-storage-fs/](./session-storage-fs) | ✅ **完整** | StorageBackend 注册 + 会话检索/标签/导出 + 自动同步 |

### P0 补齐详情（2026-09-02）

| Bundle | 补齐内容 |
|--------|----------|
| `law-search-dsh-bundle` | `cordis.patch.yml` 完整声明了 10 个 MCP server 挂载（law-search + chinese-law + 5个pkulaw + wk-mcp + filesystem预留 + law-search-cloud预留） |
| `llm-router-bundle` | `cordis.patch.yml` 声明了 4 个 Provider 配置（DeepSeek + LM Studio + Bionic SC预留 + LM Link预留），让 DSH Web UI 能展示 Provider 列表 |
| `policy-legal-safety-bundle` | `src/safety.js`（455行 CommonJS）实现了 3 层拦截逻辑；`cordis.patch.yml` 声明了工具分类表 + 授权目录 + 高危关键词配置；`package.json` 声明了 main 入口 |

### P1 补齐详情（2026-09-03）

| Bundle | 补齐内容 |
|--------|----------|
| `skill-provider-legal` | `src/provider.js`（439行 CommonJS）实现了 SkillProvider 接口：扫描 skillsDir → 格式校验 → `ctx.skills.registerProvider('legal', provider)` 动态注册；支持 autoActivate 过滤 + 定时刷新 + 降级模式 |
| `subagent-legal-bundle` | `src/subagents.js`（438行 CommonJS）实现了 3 个子代理注册：legal-retrieval（检索）/ contract-review（审查）/ litigation-drafting（起草）；每个子代理有独立 systemPrompt + 工具白名单 + 模型偏好；支持 eager/pipeline/manual 三种并行策略 |
| `session-storage-fs` | `src/storage.js`（465行 CommonJS）实现 StorageBackend 接口（append/replay）+ 会话管理器（search/get/tag/export/stats/sync）+ 自动同步定时器；通过 child_process 桥接 Python session-storage MCP 的 SQLite+FTS5 后端 |

### P2 补齐详情（2026-09-03）

| Bundle | 补齐内容 |
|--------|----------|
| `agent-loop-legal` | `src/loop.js`（413行 CommonJS）实现法律专用 Agent Loop：法条引用提取（正则匹配《法律名》第XXX条）+ 核对 + 证据覆盖率检查 + 3 种预设（standard/thorough/quick） |
| `ui-legal-citations` | `src/ui.js`（444行 CommonJS）实现 4 个 UI 组件：法条折叠卡片 + 证据标记 + 引用跳转 + 证据时间轴；ConversationNodeDefinition 注册入口 + HTML 生成函数 |
| `observability-bundle` | `src/metrics.js`（465行 CommonJS）实现 3 个指标收集器（工具/LLM/检索）+ 环形缓冲区 + 3 种导出目标（console/file/OTLP）+ 成本计算（按 Provider Token 单价） |

---

## 二、Bundle 开发流程（5 步）

**前置**：Node.js ≥ 22.19（本机 v26.5.1 ✅）、pnpm ≥ 11（或 npm 也行）、dsh 已安装。

```bash
# Step 1：创建新包目录
mkdir -p MCP/harness/plugins/<package-name>/{src,dist}
cd MCP/harness/plugins/<package-name>
npm init -y
# 然后按上述任一 P0 包的 package.json → 填入 peerDependencies / dsh.bundle / main

# Step 2：写插件核心（TS / JS 二选一，简单的用 JS 跳过编译）
cat > src/index.js <<'EOF'
'use strict';
const name = '<package-name>';
const inject = ['tools'];
async function apply(ctx) {
  // 在此注册能力：ctx.tools.register() / ctx.llm.registerAdapter() / ctx.on() / ctx.provide() ...
}
module.exports = { name, inject, apply };
EOF

# Step 3：写 cordis.patch.yml（配置层；也可以只写 apply() 不提供 patch）

# Step 4：本地 link → dsh web 热加载
npm link
# 或 用 workspace 相对依赖：profiles/<x>/package.json 里写
#   dependencies: { "@law-agent/<package-name>": "workspace:../../plugins/<package-name>" }

# Step 5：调试
dsh web   # 启动 Web UI；控制台 Fiber 面板能看到 @law-agent/<package-name> 的 apply 日志
# 变更代码后重新编译 + HMR 自动重挂（Cordis effect-scoped）
```

---

## 三、Bundle vs 普通 Plugin

| 维度 | 普通 Plugin（`dsh plugin add <name>`） | Bundle（本项目主推） |
|---|---|---|
| 格式 | npm 包导出 `apply(ctx)` | npm 包 + `package.json.dsh.bundle` 字段列出一组 plugin + patch.yml |
| 能包含配置？ | 只能代码 | **可以**（cordis.patch.yml 自动合并到当前 Profile） |
| 能包含 MCP 注册？ | 需要代码里调 mcp-client | **可以**（cordis.patch.yml 声明 server 启动命令，dsh loader 自动 fork） |
| 典型使用场景 | 单个工具（例如 text_stats） | 能力全家桶（例如 "法律检索 bundle" = MCP + 技能 + 路由 + 审批） |

我们项目所有 P0/P1 都用 Bundle，因为：
1. 每次"加能力" = 用户 `dsh plugin add` 一个包，不用手动改 5 份配置；
2. Bundle 内的 patch 是声明式的，比手写注册代码更少出错、更易审计。

---

## 四、发布 & 版本管理（建议）

当插件稳定后，走以下流程：

```bash
# 1. 进入插件目录，打版本
npm version patch
# 2. 发布到私有 registry（如公司内部 Verdaccio）或公开 npm
npm publish --access restricted
# 3. 用户端升级
dsh plugin update @law-agent/law-search-dsh-bundle
```

未发布前的开发期：直接走 profile 的 `workspace:../plugins/<name>` 相对依赖，改完保存 DSH HMR 自动刷新。

---

## 五、和 law-search Python 侧的分工约定

| 层 | 放在哪里 | 为什么 |
|---|---|---|
| 法条检索核心（Chroma / Embedding / Reranker / Evidence Fusion / Answer Guard） | Python law-search | 技术栈固定、不常变；向量库生态在 Python 最成熟 |
| 工具注册 / 模型路由 / 审批策略 / 会话存储 / 技能动态激活 / 子代理并行 / UI 定制 / 观测链 | DSH 插件（TypeScript / JavaScript / YAML） | 这些都是"高变更区"，通过插件热插拔实现 0 代码升级；避免每改一个小功能都重发 Python server |
| 接入外部 LLM 提供商 | DSH 插件（`dsh plugin add dsh-llm-<vendor>`） | 社区现成，无需改 Python |
| 接入外部工具（裁判文书网 / 工商信息 / 法院公告） | DSH 插件 + 独立 MCP Server | 避免污染 law-search 单一真相源（法条/案例/合同） |

**永远不要把「法律检索」的语义逻辑（法条匹配、证据覆盖率、法条引用校验）从 Python 搬到 DSH 插件里。** DSH 插件只负责「编排」「路由」「安全」「UI」「观测」——那些"只要写对一次就稳定不变"的核心算法，全留在 Python。

---

## 六、如何安装本项目所有插件

当 dsh 装好之后，**一条命令**就能把项目里的全部 P0/P1 插件挂到某个 Profile 上：

```bash
# 方式 A：install_mcp.sh（推荐，一步铺到 ~/.dsh）
bash MCP/harness/install_mcp.sh --apply
# 脚本内含：
#   · 把 dsh-home/ 复制到 ~/.dsh（Profiles + 全局 cordis.patch）
#   · 把 mcp_registry.json 复制到 ~/.dsh/{servers,mcp}
#   · 把 profiles/skills 链接到正确位置
#   · 把 P0 + P1 + P2 插件符号链接到 profiles/node_modules/@law-agent/

# 方式 B：手动
dsh plugin add @law-agent/law-search-dsh-bundle @law-agent/llm-router-bundle @law-agent/policy-legal-safety-bundle @law-agent/skill-provider-legal @law-agent/subagent-legal-bundle @law-agent/session-storage-fs @law-agent/agent-loop-legal @law-agent/ui-legal-citations @law-agent/observability-bundle
```

启动 dsh：

```bash
dsh --profile lm-studio-workbench web
# 或
dsh --profile work-project web
```

---

## 七、配置一致性校验

```bash
# 运行一致性检查（校验 registry / profiles / skills / P0+P1+P2 bundles 交叉引用）
python3 MCP/harness/check_config_consistency.py

# 预期输出：
# PASS ✅
# 配置一致性校验通过：
#   - registry: 10 个 MCP server
#   - router: 4 个 Provider
#   - profiles: 2 个（dsh-home 标准格式）
#   - skills: 3 个
#   - P0 bundles: 3 个（全部校验通过）
#   - P1 bundles: 3 个（全部校验通过）
#   - P2 bundles: 3 个（全部校验通过）
```
