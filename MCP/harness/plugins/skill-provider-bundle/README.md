# @law-agent/skill-provider-bundle

P1 法律领域技能注入层。

## 功能

### 静态层（cordis.patch.yml → system-prompt afterHooks，常驻 ~1.5k tokens）
- 工具调用铁律（search_law 唯一入口 + chinese-law 仅应急兜底 + 累计 ≤3 次，与 law-search server.py 方案 B 对齐）
- 法条引用格式规范（《法律名》第X条）
- 法律术语规范（应当/可以、或/且的区分）
- 安全约束（不编造法条、隐私保护、时效性标注）

### 动态层（src/index.js → ctx.skills.registerProvider()，按需激活）
- skills/*.json 领域技能包：术语表 / 引用规范 / 合同审查清单 / 文书起草规范
- 特性探测：若运行时无 skills 服务则降级为文件直读（仅日志告警，不阻断启动）

## 效果

注入约 1.5k tokens 领域上下文，使 LLM：
1. 工具选择更准确（优先免费本地工具）
2. 引用格式更规范（标准法条引用格式）
3. 安全意识更强（标注时效性、不编造法条）

## 安装

已通过 install_mcp.sh 自动链接到 DSH Desktop profiles。
