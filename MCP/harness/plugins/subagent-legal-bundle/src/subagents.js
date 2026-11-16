/**
 * @law-agent/subagent-legal-bundle — 法律子代理并行执行插件
 *
 * 通过 DSH Cordis 的 ctx.subagents.register() 接口注册 3 个专用子代理：
 *   1. legal-retrieval — 检索子代理（search_law / search_case / search_external）
 *   2. contract-review — 审查子代理（review_contract / evaluate_evidence）
 *   3. litigation-drafting — 起草子代理（write_litigation / read_document）
 *
 * 每个子代理有独立的：
 *   - 系统提示词（systemPrompt）
 *   - 工具白名单（allowedTools）和黑名单（deniedTools）
 *   - 模型偏好（modelHint.preferred / fallback）
 *   - 并发限制（maxConcurrency）和超时（timeoutSec）
 *
 * 并行策略（parallelStrategy）：
 *   - 'eager'    — 尽可能并行（所有子代理同时启动）
 *   - 'pipeline' — 流水线（检索 → 审查 → 起草，串行依赖）
 *   - 'manual'   — 手动控制（由调用方决定并行策略）
 *
 * 设计原则：
 *   - 子代理之间无状态共享（每个子代理独立上下文）
 *   - 工具白名单由 policy-legal-safety-bundle 的 Layer 1 叠加校验
 *   - 异常容错：单个子代理失败不影响其他子代理
 *   - 审计日志写入 stderr，不污染 stdout（MCP stdio 协议）
 *
 * @see MCP/HARNESS_PLUGIN_ARCHITECTURE.md — 扩展点 #7 Sub-agent Delegation
 * @see MCP/harness/plugins/README.md — 插件开发指引
 */

'use strict';

// ============================================================================
// 默认配置
// ============================================================================

const DEFAULT_PARALLEL_STRATEGY = 'eager';
const DEFAULT_LOG_LEVEL = 'warn';

/**
 * 默认子代理定义（可被 cordis.patch.yml 的 config 覆盖）
 */
const DEFAULT_AGENTS = [
  {
    name: 'legal-retrieval',
    displayName: '法律检索代理',
    description: '负责法条检索、案例检索、外部数据源查询。只读操作，可安全并行。',
    systemPrompt: [
      '你是一个专业的法律检索代理。你的职责是：',
      '1. 根据用户问题检索相关法条（search_law）',
      '2. 检索相关案例（search_case）',
      '3. 必要时查询外部数据源（search_external）',
      '',
      '工作原则：',
      '- 优先使用本地知识库（law-search MCP server）',
      '- 检索结果必须包含法条编号和原文',
      '- 案例检索需注明案号和裁判日期',
      '- 不要生成法律意见，只提供检索结果',
    ].join('\n'),
    allowedTools: ['search_law', 'search_case', 'search_external', 'chroma_status'],
    deniedTools: ['write_litigation', 'ingest_rebuild', 'review_contract'],
    modelHint: { preferred: 'lm_studio_local', fallback: 'deepseek_api' },
    maxConcurrency: 3,
    timeoutSec: 120,
  },
  {
    name: 'contract-review',
    displayName: '合同审查代理',
    description: '负责合同风险审查、条款分析、证据评估。涉及文件读取，需注意路径权限。',
    systemPrompt: [
      '你是一个专业的合同审查代理。你的职责是：',
      '1. 审查合同文件，识别风险条款（review_contract）',
      '2. 评估证据材料的证明力（evaluate_evidence）',
      '3. 生成风险清单和改进建议',
      '',
      '工作原则：',
      '- 审查结果必须引用具体条款编号',
      '- 风险等级分为：高/中/低',
      '- 每个风险点附带改进建议',
      '- 涉及路径的文件操作需在授权目录内',
    ].join('\n'),
    allowedTools: ['review_contract', 'evaluate_evidence', 'read_document', 'search_law'],
    deniedTools: ['write_litigation', 'ingest_rebuild'],
    modelHint: { preferred: 'deepseek_api', fallback: 'lm_studio_local' },
    maxConcurrency: 2,
    timeoutSec: 300,
  },
  {
    name: 'litigation-drafting',
    displayName: '文书起草代理',
    description: '负责起诉状/答辩状/代理词等法律文书的起草。写入操作，需用户确认。',
    systemPrompt: [
      '你是一个专业的法律文书起草代理。你的职责是：',
      '1. 根据案件事实和检索结果起草法律文书（write_litigation）',
      '2. 阅读参考文档获取模板和格式要求（read_document）',
      '3. 确保文书格式符合法院要求',
      '',
      '工作原则：',
      '- 文书类型包括：起诉状、答辩状、代理词、上诉状',
      '- 必须包含：当事人信息、诉讼请求、事实与理由、法律依据',
      '- 引用法条必须准确，注明条款编号',
      '- 生成文书后需经用户确认才能保存',
    ].join('\n'),
    allowedTools: ['write_litigation', 'read_document', 'search_law'],
    deniedTools: ['ingest_rebuild', 'review_contract'],
    modelHint: { preferred: 'deepseek_api', fallback: 'lm_studio_local' },
    maxConcurrency: 1,
    timeoutSec: 600,
  },
];

// ============================================================================
// 日志工具
// ============================================================================

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };

function createLogger(level) {
  const threshold = LOG_LEVELS[level] !== undefined ? LOG_LEVELS[level] : LOG_LEVELS.warn;
  return {
    error: (msg) => { if (threshold >= 0) process.stderr.write(`[subagent-legal] [ERROR] ${msg}\n`); },
    warn:  (msg) => { if (threshold >= 1) process.stderr.write(`[subagent-legal] [WARN]  ${msg}\n`); },
    info:  (msg) => { if (threshold >= 2) process.stderr.write(`[subagent-legal] [INFO]  ${msg}\n`); },
    debug: (msg) => { if (threshold >= 3) process.stderr.write(`[subagent-legal] [DEBUG] ${msg}\n`); },
  };
}

// ============================================================================
// 子代理定义校验
// ============================================================================

/**
 * 校验单个子代理定义的格式
 * 返回 { valid: boolean, errors: string[] }
 */
function validateAgentDef(agent) {
  const errors = [];

  if (!agent.name || typeof agent.name !== 'string') {
    errors.push('缺少 name 字段');
  } else if (!/^[a-z0-9-]+$/.test(agent.name)) {
    errors.push(`name 格式错误（只允许小写字母/数字/连字符）: ${agent.name}`);
  }

  if (!agent.systemPrompt || typeof agent.systemPrompt !== 'string') {
    errors.push('缺少 systemPrompt 字段');
  }

  if (agent.allowedTools && !Array.isArray(agent.allowedTools)) {
    errors.push('allowedTools 应为数组');
  }

  if (agent.deniedTools && !Array.isArray(agent.deniedTools)) {
    errors.push('deniedTools 应为数组');
  }

  if (agent.modelHint) {
    if (typeof agent.modelHint !== 'object') {
      errors.push('modelHint 应为对象');
    } else {
      if (!agent.modelHint.preferred && !agent.modelHint.fallback) {
        errors.push('modelHint 至少需要 preferred 或 fallback');
      }
    }
  }

  if (agent.maxConcurrency !== undefined) {
    if (typeof agent.maxConcurrency !== 'number' || agent.maxConcurrency < 1) {
      errors.push('maxConcurrency 应为正整数');
    }
  }

  if (agent.timeoutSec !== undefined) {
    if (typeof agent.timeoutSec !== 'number' || agent.timeoutSec < 1) {
      errors.push('timeoutSec 应为正整数');
    }
  }

  return { valid: errors.length === 0, errors };
}

// ============================================================================
// 子代理 Provider 实现
// ============================================================================

/**
 * 创建子代理配置
 * 返回 SubagentProvider 兼容对象
 */
function createSubagentConfig(agentDef) {
  return {
    name: agentDef.name,
    displayName: agentDef.displayName || agentDef.name,
    description: agentDef.description || '',

    /**
     * 获取子代理的系统提示词
     */
    getSystemPrompt() {
      return agentDef.systemPrompt;
    },

    /**
     * 工具过滤：返回允许的工具列表
     * 与 policy-legal-safety-bundle 的 Layer 1 叠加校验
     */
    filterTools(allTools) {
      const allowed = new Set(agentDef.allowedTools || []);
      const denied = new Set(agentDef.deniedTools || []);

      return allTools.filter(tool => {
        const toolName = typeof tool === 'string' ? tool : (tool.name || tool.toolName);
        // 黑名单优先
        if (denied.has(toolName)) return false;
        // 白名单为空则允许所有
        if (allowed.size === 0) return true;
        return allowed.has(toolName);
      });
    },

    /**
     * 模型选择提示
     */
    getModelHint() {
      return agentDef.modelHint || {};
    },

    /**
     * 并发限制
     */
    getMaxConcurrency() {
      return agentDef.maxConcurrency || 1;
    },

    /**
     * 超时时间（秒）
     */
    getTimeoutSec() {
      return agentDef.timeoutSec || 300;
    },

    /**
     * 原始定义（供调试和测试使用）
     */
    _raw: agentDef,
  };
}

/**
 * 创建 SubagentProvider 实例
 */
function createSubagentProvider(agents, logger) {
  const agentMap = new Map();

  for (const agentDef of agents) {
    const validation = validateAgentDef(agentDef);
    if (!validation.valid) {
      logger.warn(`子代理 ${agentDef.name || '(unnamed)'} 定义无效: ${validation.errors.join('; ')}`);
      continue;
    }

    const config = createSubagentConfig(agentDef);
    agentMap.set(agentDef.name, config);
    logger.info(`子代理已注册: ${agentDef.name} (${agentDef.displayName})`);
  }

  return {
    /**
     * list() — 返回所有已注册的子代理元数据
     */
    async list() {
      const result = [];
      for (const [name, config] of agentMap) {
        result.push({
          name: config.name,
          displayName: config.displayName,
          description: config.description,
          modelHint: config.getModelHint(),
          maxConcurrency: config.getMaxConcurrency(),
          timeoutSec: config.getTimeoutSec(),
        });
      }
      return result;
    },

    /**
     * get(name) — 按名称获取子代理配置
     */
    async get(agentName) {
      return agentMap.get(agentName) || null;
    },

    /**
     * _internal — 内部函数导出供单元测试使用
     */
    _internal: {
      getAgentMap: () => agentMap,
      validateAgentDef,
      createSubagentConfig,
    },
  };
}

// ============================================================================
// 并行执行辅助
// ============================================================================

/**
 * 并行执行多个子代理任务
 *
 * @param {object} provider - SubagentProvider 实例
 * @param {Array<{agentName: string, task: string, args?: object}>} tasks - 任务列表
 * @param {string} strategy - 并行策略（'eager' / 'pipeline' / 'manual'）
 * @param {Function} executeFn - 执行函数 (agentConfig, task) => Promise<result>
 * @param {object} logger - 日志对象
 * @returns {Promise<Map<agentName, result>>}
 */
async function executeParallel(provider, tasks, strategy, executeFn, logger) {
  const results = new Map();

  if (strategy === 'eager') {
    // 尽可能并行：所有任务同时启动
    logger.info(`并行策略: eager — 同时启动 ${tasks.length} 个子代理任务`);
    const promises = tasks.map(async (task) => {
      const agentConfig = await provider.get(task.agentName);
      if (!agentConfig) {
        logger.warn(`子代理不存在: ${task.agentName}`);
        results.set(task.agentName, { error: `子代理不存在: ${task.agentName}` });
        return;
      }
      try {
        const result = await executeFn(agentConfig, task);
        results.set(task.agentName, result);
      } catch (err) {
        logger.error(`子代理 ${task.agentName} 执行失败: ${err.message}`);
        results.set(task.agentName, { error: err.message });
      }
    });
    await Promise.all(promises);

  } else if (strategy === 'pipeline') {
    // 流水线：按任务顺序串行执行
    logger.info(`并行策略: pipeline — 串行执行 ${tasks.length} 个子代理任务`);
    for (const task of tasks) {
      const agentConfig = await provider.get(task.agentName);
      if (!agentConfig) {
        logger.warn(`子代理不存在: ${task.agentName}`);
        results.set(task.agentName, { error: `子代理不存在: ${task.agentName}` });
        continue;
      }
      try {
        const result = await executeFn(agentConfig, task);
        results.set(task.agentName, result);
      } catch (err) {
        logger.error(`子代理 ${task.agentName} 执行失败: ${err.message}`);
        results.set(task.agentName, { error: err.message });
      }
    }

  } else {
    // manual: 不自动执行，由调用方控制
    logger.info(`并行策略: manual — 不自动执行，由调用方控制`);
  }

  return results;
}

// ============================================================================
// Cordis 插件入口
// ============================================================================

const name = 'subagent-legal';
const inject = ['subagents'];

/**
 * 插件 apply 函数 — 注册子代理
 *
 * @param {object} ctx — Cordis Context
 */
async function apply(ctx, pluginConfig) {
  pluginConfig = pluginConfig || {}; // 0.2.2-rc.1: config 由 apply 第二参数注入

  const agents = pluginConfig.agents || DEFAULT_AGENTS;
  const parallelStrategy = pluginConfig.parallelStrategy || DEFAULT_PARALLEL_STRATEGY;
  const logLevel = pluginConfig.logLevel || DEFAULT_LOG_LEVEL;

  const logger = createLogger(logLevel);
  logger.info(`插件初始化: ${agents.length} 个子代理, 并行策略=${parallelStrategy}`);

  // 创建 SubagentProvider
  const provider = createSubagentProvider(agents, logger);

  // 注册到 Cordis
  if (ctx.subagents && typeof ctx.subagents.register === 'function') {
    ctx.subagents.register('legal', provider);
    logger.info('SubagentProvider 已注册: ctx.subagents.register("legal", ...)');
  } else {
    // DSH 版本可能还没有 ctx.subagents API，降级为日志提示
    logger.warn('ctx.subagents.register 不可用 — 当前 DSH 版本可能不支持 Sub-agent API');
    logger.warn('降级模式：子代理定义已加载但无法注册到 DSH 运行时');

    // 降级方案：把子代理列表写入 stderr 供调试参考
    const list = await provider.list();
    for (const meta of list) {
      logger.info(`  [降级] 子代理可用: ${meta.name} — ${meta.displayName} (并发=${meta.maxConcurrency}, 超时=${meta.timeoutSec}s)`);
    }
  }

  // 注册并行执行辅助函数（供其他插件或主代理调用）
  if (ctx.provide && typeof ctx.provide === 'function') {
    ctx.provide('subagent-legal:executeParallel', (tasks, executeFn) => {
      return executeParallel(provider, tasks, parallelStrategy, executeFn, logger);
    });
    logger.info('并行执行辅助函数已注册: ctx.provide("subagent-legal:executeParallel", ...)');
  }

  logger.info('插件加载完成');
}

// ============================================================================
// 导出（CommonJS，兼容 Cordis loader）
// ============================================================================

module.exports = {
  name,
  inject,
  apply,
  // 内部函数导出供单元测试使用
  _internal: {
    createSubagentProvider,
    createSubagentConfig,
    createLogger,
    validateAgentDef,
    executeParallel,
    DEFAULT_AGENTS,
    DEFAULT_PARALLEL_STRATEGY,
    DEFAULT_LOG_LEVEL,
  },
};
