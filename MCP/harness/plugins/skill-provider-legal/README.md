# @law-agent/skill-provider-legal

> **P1 Plugin** — 法律技能动态激活插件

## 定位

将 `MCP/harness/skills/*.json` 文件通过 DSH Cordis 的 `ctx.skills.registerProvider()` 接口动态注册为 SkillProvider，替代静态 JSON 卡片方案。

**改造前**：`skills/*.json` 只是静态描述文件，DSH 运行时不会主动发现和加载它们。

**改造后**：本插件在 DSH 启动时扫描技能目录，将每个 JSON 文件解析为技能定义，注册为 `legal` Provider，让 DSH 可以按 Profile 动态激活对应技能。

---

## 功能

| 功能 | 说明 |
|------|------|
| 技能扫描 | 启动时扫描 `skillsDir` 下所有 `*.json` 文件 |
| 格式校验 | 校验 `skill_id` / `skill_name` / `version` 必填字段 |
| 动态注册 | 通过 `ctx.skills.registerProvider('legal', provider)` 注册 |
| autoActivate 过滤 | 按 Profile 配置的白名单过滤激活哪些技能 |
| 异常容错 | 单个 JSON 解析失败不影响其他技能加载 |
| 定时刷新 | 可选的文件监视热刷新（`refreshIntervalSec > 0`） |
| 降级模式 | DSH 版本不支持 SkillProvider API 时，降级为日志输出 |

---

## 当前技能清单

| skill_id | 名称 | 版本 | 挂载工作台 |
|----------|------|------|-----------|
| `law-search-comprehensive` | 中国法律综合检索与分析 | 0.2.0 | lm_studio_workbench |
| `contract-review` | 合同风险审查与条款改进 | 0.3.0 | lm_studio_workbench |
| `litigation-drafting` | 法律文书起草（起诉状/答辩状/代理词） | 0.2.0 | bionic_work_project |

---

## 配置（cordis.patch.yml）

```yaml
- insert:
    - id: skill-provider-legal
      name: '@law-agent/skill-provider-legal'
      config:
        skillsDir: /path/to/skills           # 技能文件目录
        refreshIntervalSec: 0                 # 0=禁用自动刷新
        logLevel: warn                        # error/warn/info/debug
        autoActivate:                         # 可选：白名单过滤
          - law-search-comprehensive
          - contract-review
```

---

## SkillProvider 接口

```javascript
// list() → Promise<SkillMeta[]>
await provider.list();
// [
//   { id: 'law-search-comprehensive', name: '中国法律综合检索与分析', version: '0.2.0', ... },
//   { id: 'contract-review', name: '合同风险审查与条款改进', version: '0.3.0', ... },
// ]

// get(skillId) → Promise<SkillDefinition | null>
await provider.get('contract-review');
// { id, name, version, workflowSteps: [...], raw: {...}, ... }
```

---

## 单元测试

```javascript
const { _internal } = require('./src/provider');
const { validateSkillJson, createSkillProvider, createLogger } = _internal;

const logger = createLogger('debug');

// 测试格式校验
const result = validateSkillJson({ skill_id: 'test', skill_name: 'Test', version: '1.0.0' });
console.assert(result.valid === true);

// 测试 Provider
const provider = createSkillProvider({
  skillsDir: '/path/to/skills',
  autoActivate: null,
  logger,
});
provider.list().then(skills => console.log(skills));
```

---

## 与 Profile 的集成

每个 Profile 的 `cordis.patch.yml` 可以声明 `autoActivate` 列表，控制该工作台激活哪些技能：

- **lm-studio-workbench**：激活 `law-search-comprehensive` + `contract-review`
- **work-project**：激活 `litigation-drafting` + `contract-review`

---

## 设计原则

1. **与 Python 侧零耦合** — 技能定义是纯 JSON，不涉及 Python 代码
2. **异常容错** — 单个文件解析失败不影响其他技能
3. **降级兼容** — DSH 版本不支持 SkillProvider API 时降级为日志输出
4. **审计日志** — 写入 stderr，不污染 MCP stdio 协议
