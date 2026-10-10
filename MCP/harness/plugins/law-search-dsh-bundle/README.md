# @law-agent/law-search-dsh-bundle

> P0 必装 Bundle：把 law-search 整套能力（MCP server / 默认技能 / 工具桥）一次性挂进 DSH 任意 Profile。

## 包含内容

- **MCP server：law-search**（stdio transport）—— 自动启动 law-search/server.py，暴露 7 个工具：`search_law / search_case / chroma_status / ingest_rebuild / write_litigation / review_contract / evaluate_evidence`。
- **MCP server：filesystem**（可选，按需启用）—— 授权目录：`1-Work` + `uploads/`。
- **默认技能激活**：`law_search_skill / contract_review_skill / litigation_drafting_skill`（项目原 skills/*.json 转 DSH SkillProvider 的 P1 插件加载前，先用 system prompt 注入其描述）。
- **cordis.patch.yml**：无需用户手工写 MCP 注册条目，装完 bundle 自动生效。

## 安装方式

```bash
# 开发期（项目 workspace 内 profile 的 package.json 已写好 workspace: 相对依赖；npm install 即可）
cd MCP
npm install

# 发布期（发布到 npm/私有 registry 后）
dsh plugin add @law-agent/law-search-dsh-bundle
```

## 调试：本地开发 + DSH 热加载

```bash
# 1. 进入本目录，链接到全局 dsh 环境
cd MCP/harness/plugins/law-search-dsh-bundle
npm link

# 2. 在 dsh web 的 profile 里安装（若未写进 profiles/<x>/package.json）
dsh plugin add @law-agent/law-search-dsh-bundle

# 3. 观察 MCP server 连接状态（dsh 控制台 → MCP Servers 面板）
#    law-search 应为绿色，7 个工具显示在 Tools 列表
```

## 为什么这样设计

- 把「MCP server 注册 + 技能 + 工具桥」打包成一个 bundle：用户不需要分别手动配置 mcp_registry、技能卡片、默认策略，**dsh plugin add 一个包 → 全能力就绪**，符合"一切皆插件"的理念。
- 业务代码仍在 Python law-search 里，bundle 只是做「Cordis 注册」和「启动参数构造」的薄壳，**不复制 Python 逻辑**，避免维护双份。
