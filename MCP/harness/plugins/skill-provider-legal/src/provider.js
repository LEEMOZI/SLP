/**
 * @law-agent/skill-provider-legal — 法律技能动态激活插件
 *
 * 实现 DSH SkillProvider 接口，将 skills/*.json 文件动态注册为 DSH 可识别的技能包。
 *
 * 核心职责：
 *   1. 启动时扫描 skillsDir 下所有 *.json 文件
 *   2. 解析每个文件的 skill_id / skill_name / version / workflow_steps / dependencies
 *   3. 通过 ctx.skills.registerProvider() 注册为 SkillProvider
 *   4. 支持按 Profile 的 autoActivate 列表过滤激活哪些技能
 *   5. 可选的文件监视热刷新（refreshIntervalSec > 0 时启用）
 *
 * SkillProvider 接口：
 *   list() → Promise<SkillMeta[]>  — 返回所有可用技能的元数据
 *   get(skillId) → Promise<SkillDefinition | null>  — 按 ID 返回完整技能定义
 *
 * 设计原则：
 *   - 与 Python law-search 侧零耦合（技能定义是纯 JSON，不涉及 Python）
 *   - 异常容错：单个 JSON 解析失败不影响其他技能加载
 *   - 审计日志写入 stderr（DSH 控制台可见），不污染 stdout（MCP stdio 协议）
 *
 * @see MCP/HARNESS_PLUGIN_ARCHITECTURE.md — 扩展点 #6 Skill Provider
 * @see MCP/harness/plugins/README.md — 插件开发指引
 */

'use strict';

const fs = require('fs');
const path = require('path');

// ============================================================================
// 默认配置
// ============================================================================

const DEFAULT_SKILLS_DIR = '/Users/simon/Documents/1-SLP/MCP/harness/skills';
const DEFAULT_REFRESH_INTERVAL_SEC = 0;
const DEFAULT_LOG_LEVEL = 'warn';

/**
 * 技能 JSON 的必填字段校验清单
 */
const REQUIRED_FIELDS = ['skill_id', 'skill_name', 'version'];

/**
 * 技能 JSON 的可选字段（存在时进行格式校验）
 */
const OPTIONAL_FIELDS = [
  '_comment', 'dependencies', 'workflow_steps', 'status', 'attach_to',
];

// ============================================================================
// 日志工具
// ============================================================================

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };

function createLogger(level) {
  const threshold = LOG_LEVELS[level] !== undefined ? LOG_LEVELS[level] : LOG_LEVELS.warn;
  return {
    error: (msg) => { if (threshold >= 0) process.stderr.write(`[skill-provider-legal] [ERROR] ${msg}\n`); },
    warn:  (msg) => { if (threshold >= 1) process.stderr.write(`[skill-provider-legal] [WARN]  ${msg}\n`); },
    info:  (msg) => { if (threshold >= 2) process.stderr.write(`[skill-provider-legal] [INFO]  ${msg}\n`); },
    debug: (msg) => { if (threshold >= 3) process.stderr.write(`[skill-provider-legal] [DEBUG] ${msg}\n`); },
  };
}

// ============================================================================
// 技能文件解析
// ============================================================================

/**
 * 校验单个技能 JSON 的格式
 * 返回 { valid: boolean, errors: string[] }
 */
function validateSkillJson(data, filePath) {
  const errors = [];

  // 必填字段
  for (const field of REQUIRED_FIELDS) {
    if (!data[field]) {
      errors.push(`缺少必填字段: ${field}`);
    }
  }

  // skill_id 格式：只允许小写字母、数字、连字符
  if (data.skill_id && !/^[a-z0-9-]+$/.test(data.skill_id)) {
    errors.push(`skill_id 格式错误（只允许小写字母/数字/连字符）: ${data.skill_id}`);
  }

  // version 格式：semver 简化校验
  if (data.version && !/^\d+\.\d+\.\d+/.test(data.version)) {
    errors.push(`version 格式错误（应为 semver）: ${data.version}`);
  }

  // dependencies 格式校验
  if (data.dependencies) {
    if (typeof data.dependencies !== 'object' || Array.isArray(data.dependencies)) {
      errors.push('dependencies 应为对象');
    } else {
      if (data.dependencies.mcp_servers && !Array.isArray(data.dependencies.mcp_servers)) {
        errors.push('dependencies.mcp_servers 应为数组');
      }
      if (data.dependencies.model_provider && !Array.isArray(data.dependencies.model_provider)) {
        errors.push('dependencies.model_provider 应为数组');
      }
    }
  }

  // workflow_steps 格式校验
  if (data.workflow_steps) {
    if (!Array.isArray(data.workflow_steps)) {
      errors.push('workflow_steps 应为数组');
    } else {
      data.workflow_steps.forEach((step, i) => {
        if (!step.step || !step.action) {
          errors.push(`workflow_steps[${i}] 缺少 step 或 action 字段`);
        }
      });
    }
  }

  // attach_to 格式校验
  if (data.attach_to && !Array.isArray(data.attach_to)) {
    errors.push('attach_to 应为数组');
  }

  return {
    valid: errors.length === 0,
    errors,
  };
}

/**
 * 从单个 JSON 文件加载技能定义
 * 返回 { skillId, skillMeta, skillDef, error? }
 */
function loadSkillFromFile(filePath, logger) {
  let raw;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch (err) {
    return { error: `文件读取失败: ${err.message}` };
  }

  let data;
  try {
    data = JSON.parse(raw);
  } catch (err) {
    return { error: `JSON 解析失败: ${err.message}` };
  }

  const validation = validateSkillJson(data, filePath);
  if (!validation.valid) {
    return { error: `格式校验失败: ${validation.errors.join('; ')}` };
  }

  const skillId = data.skill_id;

  // 提取元数据（轻量，供 list() 使用）
  const skillMeta = {
    id: skillId,
    name: data.skill_name,
    version: data.version,
    description: data._comment || '',
    status: data.status || 'active',
    attachTo: data.attach_to || [],
    dependencies: {
      mcpServers: (data.dependencies && data.dependencies.mcp_servers) || [],
      modelProviders: (data.dependencies && data.dependencies.model_provider) || [],
    },
  };

  // 完整定义（供 get() 使用）
  const skillDef = {
    ...skillMeta,
    workflowSteps: data.workflow_steps || [],
    raw: data,  // 保留原始 JSON 供下游消费
  };

  logger.debug(`加载技能: ${skillId} (v${data.version}) from ${path.basename(filePath)}`);
  return { skillId, skillMeta, skillDef };
}

/**
 * 扫描目录下所有 *.json 文件，加载技能
 * 返回 { skills: Map<skillId, { meta, def }>, errors: string[] }
 */
function scanSkillsDir(skillsDir, logger) {
  const skills = new Map();
  const errors = [];

  if (!fs.existsSync(skillsDir)) {
    errors.push(`技能目录不存在: ${skillsDir}`);
    return { skills, errors };
  }

  let files;
  try {
    files = fs.readdirSync(skillsDir).filter(f => f.endsWith('.json'));
  } catch (err) {
    errors.push(`目录读取失败: ${err.message}`);
    return { skills, errors };
  }

  logger.info(`扫描技能目录: ${skillsDir}（${files.length} 个 JSON 文件）`);

  for (const file of files) {
    const filePath = path.join(skillsDir, file);
    const result = loadSkillFromFile(filePath, logger);

    if (result.error) {
      errors.push(`${file}: ${result.error}`);
      logger.warn(`跳过 ${file}: ${result.error}`);
      continue;
    }

    if (skills.has(result.skillId)) {
      errors.push(`${file}: skill_id=${result.skillId} 重复（已加载）`);
      logger.warn(`跳过 ${file}: skill_id=${result.skillId} 重复`);
      continue;
    }

    skills.set(result.skillId, { meta: result.skillMeta, def: result.skillDef });
    logger.info(`已加载技能: ${result.skillMeta.name} (${result.skillId} v${result.skillMeta.version})`);
  }

  return { skills, errors };
}

// ============================================================================
// SkillProvider 实现
// ============================================================================

/**
 * 创建 SkillProvider 实例
 *
 * @param {object} options
 * @param {string} options.skillsDir - 技能文件目录
 * @param {string[]} [options.autoActivate] - 要激活的 skill_id 列表（空=全部激活）
 * @param {object} options.logger - 日志对象
 * @returns {{ list: Function, get: Function, _refresh: Function, _internal: object }}
 */
function createSkillProvider(options) {
  const { skillsDir, autoActivate, logger } = options;
  let skillsCache = new Map();
  let loadErrors = [];

  // 首次加载
  function refresh() {
    const result = scanSkillsDir(skillsDir, logger);
    skillsCache = result.skills;
    loadErrors = result.errors;

    // 如果有 autoActivate 过滤，记录哪些被过滤掉了
    if (autoActivate && autoActivate.length > 0) {
      const activated = [];
      const skipped = [];
      for (const [id, entry] of skillsCache) {
        if (autoActivate.includes(id)) {
          activated.push(id);
        } else {
          skipped.push(id);
        }
      }
      if (skipped.length > 0) {
        logger.info(`autoActivate 过滤: 激活 ${activated.length} 个，跳过 ${skipped.length} 个 (${skipped.join(', ')})`);
      }
    }

    logger.info(`技能加载完成: ${skillsCache.size} 个可用${loadErrors.length > 0 ? `，${loadErrors.length} 个错误` : ''}`);
  }

  refresh();

  return {
    /**
     * list() — 返回所有可用技能的元数据
     * 如果有 autoActivate 过滤，只返回被激活的技能
     */
    async list() {
      const result = [];
      for (const [id, entry] of skillsCache) {
        // autoActivate 过滤
        if (autoActivate && autoActivate.length > 0 && !autoActivate.includes(id)) {
          continue;
        }
        result.push(entry.meta);
      }
      return result;
    },

    /**
     * get(skillId) — 按 ID 返回完整技能定义
     * 如果技能不存在或被 autoActivate 过滤，返回 null
     */
    async get(skillId) {
      const entry = skillsCache.get(skillId);
      if (!entry) {
        logger.debug(`get(${skillId}): 未找到`);
        return null;
      }

      // autoActivate 过滤
      if (autoActivate && autoActivate.length > 0 && !autoActivate.includes(skillId)) {
        logger.debug(`get(${skillId}): 被 autoActivate 过滤`);
        return null;
      }

      return entry.def;
    },

    /**
     * _refresh() — 手动触发重新加载（供测试或文件监视使用）
     */
    _refresh() {
      logger.info('手动触发技能刷新...');
      refresh();
    },

    /**
     * _internal — 内部函数导出供单元测试使用
     */
    _internal: {
      getCache: () => skillsCache,
      getErrors: () => loadErrors,
      validateSkillJson,
      loadSkillFromFile,
      scanSkillsDir,
    },
  };
}

// ============================================================================
// 文件监视（可选，refreshIntervalSec > 0 时启用）
// ============================================================================

/**
 * 启动定时刷新
 * 返回 stop 函数
 */
function startPeriodicRefresh(provider, intervalSec, logger) {
  if (!intervalSec || intervalSec <= 0) {
    return () => {};  // no-op
  }

  logger.info(`启用技能文件定时刷新: 每 ${intervalSec} 秒`);
  const timer = setInterval(() => {
    try {
      provider._refresh();
    } catch (err) {
      logger.error(`定时刷新失败: ${err.message}`);
    }
  }, intervalSec * 1000);

  // 返回 stop 函数
  return () => {
    clearInterval(timer);
    logger.info('技能文件定时刷新已停止');
  };
}

// ============================================================================
// Cordis 插件入口
// ============================================================================

const name = 'skill-provider-legal';
const inject = ['skills'];

/**
 * 插件 apply 函数 — 注册 SkillProvider
 *
 * @param {object} ctx — Cordis Context
 */
async function apply(ctx, pluginConfig) {
  pluginConfig = pluginConfig || {}; // 0.2.2-rc.1: config 由 apply 第二参数注入

  const skillsDir = pluginConfig.skillsDir || DEFAULT_SKILLS_DIR;
  const refreshIntervalSec = pluginConfig.refreshIntervalSec || DEFAULT_REFRESH_INTERVAL_SEC;
  const logLevel = pluginConfig.logLevel || DEFAULT_LOG_LEVEL;
  const autoActivate = pluginConfig.autoActivate || null;

  const logger = createLogger(logLevel);
  logger.info(`插件初始化: skillsDir=${skillsDir}, refresh=${refreshIntervalSec}s, logLevel=${logLevel}`);

  // 创建 SkillProvider
  const provider = createSkillProvider({ skillsDir, autoActivate, logger });

  // 注册到 Cordis
  if (ctx.skills && typeof ctx.skills.registerProvider === 'function') {
    try {
      ctx.skills.registerProvider('legal', provider);
      logger.info('SkillProvider 已注册: ctx.skills.registerProvider("legal", ...)');
    } catch (err) {
      // 0.2.2-rc.1 起 registerProvider 改为接收 factory 函数，旧签名会抛 TypeError。
      // 降级为仅加载技能文件，不阻断插件激活与会话创建。
      logger.warn(`SkillProvider 注册失败（DSH API 变更），进入降级模式: ${err.message}`);
    }
  } else {
    // DSH 版本可能还没有 ctx.skills API，降级为日志提示
    logger.warn('ctx.skills.registerProvider 不可用 — 当前 DSH 版本可能不支持 SkillProvider API');
    logger.warn('降级模式：技能文件已加载但无法注册到 DSH 运行时');

    // 降级方案：把技能列表写入 stderr 供调试参考
    const list = await provider.list();
    for (const meta of list) {
      logger.info(`  [降级] 技能可用: ${meta.id} — ${meta.name} (v${meta.version})`);
    }
  }

  // 启动定时刷新（如果配置了）
  const stopRefresh = startPeriodicRefresh(provider, refreshIntervalSec, logger);

  // 注册插件卸载钩子（如果 Cordis 支持）
  if (ctx.on && typeof ctx.on === 'function') {
    ctx.on('plugin:dispose', () => {
      stopRefresh();
      logger.info('插件已卸载');
    });
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
    createSkillProvider,
    createLogger,
    validateSkillJson,
    loadSkillFromFile,
    scanSkillsDir,
    startPeriodicRefresh,
    DEFAULT_SKILLS_DIR,
    DEFAULT_REFRESH_INTERVAL_SEC,
    DEFAULT_LOG_LEVEL,
    REQUIRED_FIELDS,
  },
};
