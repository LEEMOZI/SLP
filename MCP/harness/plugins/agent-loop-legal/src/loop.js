/**
 * @law-agent/agent-loop-legal — 法律专用 Agent Loop 插件
 *
 * 通过 DSH Cordis 的 ctx.agent.loop 接口替换默认思考-行动循环，
 * 在标准 plan → tool → re-plan → done 流程中插入法律专用步骤：
 *
 *   1. 法条引用核对（Citation Verification）
 *      - 从回答中提取法条引用（如"《民法典》第XXX条"）
 *      - 与检索结果中的法条原文比对
 *      - 标记不一致的引用（法条编号错误或原文不匹配）
 *
 *   2. 证据覆盖率检查（Evidence Coverage Check）
 *      - 分析检索到的关键证据点
 *      - 检查回答是否覆盖了这些证据点
 *      - 计算覆盖率百分比，低于阈值时触发警告
 *
 * 设计原则：
 *   - 当前 Python answer_guard 已实现核心逻辑（core/answer_guard.py）
 *   - 本插件在 DSH 层提供编排入口，为未来从 Python 迁移到 DSH 做准备
 *   - 不破坏现有链路：Python 侧的 answer_guard 继续工作，本插件作为补充
 *   - 异常容错：插件自身错误不阻断主流程
 *
 * Loop 策略预设：
 *   - standard: 标准法律检索（默认）
 *   - thorough: 深度审查（增加多轮覆盖率检查）
 *   - quick: 快速回答（跳过覆盖率检查）
 *
 * @see MCP/HARNESS_PLUGIN_ARCHITECTURE.md — 扩展点 #5 Agent Loop
 * @see MCP/law-search/core/answer_guard.py — Python 侧实现
 */

'use strict';

// ============================================================================
// 默认配置
// ============================================================================

const DEFAULT_PRESET = 'standard';
const DEFAULT_LOG_LEVEL = 'warn';

/**
 * 法条引用正则表达式（匹配中国法律引用格式）
 * 示例：《民法典》第143条、《刑法》第二百六十四条、《合同法》第60条第1款
 */
const CITATION_PATTERNS = [
  // 《法律名》第XXX条
  /《([^》]+)》第([零一二三四五六七八九十百千\d]+)条/g,
  // 《法律名》第XXX条第X款
  /《([^》]+)》第([零一二三四五六七八九十百千\d]+)条第([零一二三四五六七八九十\d]+)款/g,
  // 《法律名》第XXX条之X
  /《([^》]+)》第([零一二三四五六七八九十百千\d]+)条之([零一二三四五六七八九十\d]+)/g,
];

/**
 * 预设配置
 */
const PRESETS = {
  standard: {
    citationVerification: true,
    evidenceCoverage: true,
    coverageThreshold: 0.6,
    maxRounds: 1,
  },
  thorough: {
    citationVerification: true,
    evidenceCoverage: true,
    coverageThreshold: 0.8,
    maxRounds: 3,
  },
  quick: {
    citationVerification: true,
    evidenceCoverage: false,
    coverageThreshold: 0,
    maxRounds: 1,
  },
};

// ============================================================================
// 日志工具
// ============================================================================

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };

function createLogger(level) {
  const threshold = LOG_LEVELS[level] !== undefined ? LOG_LEVELS[level] : LOG_LEVELS.warn;
  return {
    error: (msg) => { if (threshold >= 0) process.stderr.write(`[agent-loop-legal] [ERROR] ${msg}\n`); },
    warn:  (msg) => { if (threshold >= 1) process.stderr.write(`[agent-loop-legal] [WARN]  ${msg}\n`); },
    info:  (msg) => { if (threshold >= 2) process.stderr.write(`[agent-loop-legal] [INFO]  ${msg}\n`); },
    debug: (msg) => { if (threshold >= 3) process.stderr.write(`[agent-loop-legal] [DEBUG] ${msg}\n`); },
  };
}

// ============================================================================
// 法条引用提取
// ============================================================================

/**
 * 从文本中提取所有法条引用
 * 返回 [{ lawName, articleNumber, paragraph?, fullMatch }]
 */
function extractCitations(text) {
  const citations = [];
  if (!text || typeof text !== 'string') return citations;

  for (const pattern of CITATION_PATTERNS) {
    // 重置正则状态
    pattern.lastIndex = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      citations.push({
        lawName: match[1],
        articleNumber: match[2],
        paragraph: match[3] || null,
        fullMatch: match[0],
        index: match.index,
      });
    }
  }

  // 去重（按 fullMatch）
  const seen = new Set();
  return citations.filter(c => {
    if (seen.has(c.fullMatch)) return false;
    seen.add(c.fullMatch);
    return true;
  });
}

// ============================================================================
// 法条引用核对
// ============================================================================

/**
 * 核对回答中的法条引用是否与检索结果一致
 *
 * @param {string} response - 生成的回答文本
 * @param {Array} searchResults - 检索结果（包含法条原文）
 * @param {object} logger - 日志对象
 * @returns {{ verified: number, mismatched: number, warnings: string[] }}
 */
function verifyCitations(response, searchResults, logger) {
  const citations = extractCitations(response);
  logger.info(`发现 ${citations.length} 个法条引用`);

  if (citations.length === 0) {
    return { verified: 0, mismatched: 0, warnings: [] };
  }

  // 构建检索结果中的法条索引
  const lawIndex = new Map();
  if (Array.isArray(searchResults)) {
    for (const result of searchResults) {
      const lawName = result.law_name || result.lawName || '';
      const articleNum = result.article_number || result.articleNumber || '';
      if (lawName && articleNum) {
        const key = `${lawName}:${articleNum}`;
        lawIndex.set(key, result);
      }
    }
  }

  let verified = 0;
  let mismatched = 0;
  const warnings = [];

  for (const citation of citations) {
    const key = `${citation.lawName}:${citation.articleNumber}`;
    const matched = lawIndex.get(key);

    if (matched) {
      verified++;
      logger.debug(`引用核对通过: ${citation.fullMatch}`);
    } else {
      mismatched++;
      const warning = `法条引用未找到: ${citation.fullMatch}（可能在检索结果中不存在）`;
      warnings.push(warning);
      logger.warn(warning);
    }
  }

  return { verified, mismatched, warnings };
}

// ============================================================================
// 证据覆盖率检查
// ============================================================================

/**
 * 检查回答是否覆盖了检索到的关键证据点
 *
 * @param {string} response - 生成的回答文本
 * @param {Array} evidencePoints - 关键证据点列表
 * @param {number} threshold - 覆盖率阈值
 * @param {object} logger - 日志对象
 * @returns {{ coverage: number, covered: number, total: number, missing: string[] }}
 */
function checkEvidenceCoverage(response, evidencePoints, threshold, logger) {
  if (!Array.isArray(evidencePoints) || evidencePoints.length === 0) {
    logger.info('无证据点需要检查覆盖率');
    return { coverage: 1.0, covered: 0, total: 0, missing: [] };
  }

  const responseLower = response.toLowerCase();
  let covered = 0;
  const missing = [];

  for (const point of evidencePoints) {
    const pointText = typeof point === 'string' ? point : (point.text || point.content || '');
    if (!pointText) continue;

    // 简单的关键词匹配（实际实现可能需要更复杂的语义匹配）
    const keywords = extractKeywords(pointText);
    const matchCount = keywords.filter(kw => responseLower.includes(kw.toLowerCase())).length;
    const matchRatio = keywords.length > 0 ? matchCount / keywords.length : 0;

    if (matchRatio >= 0.5) {
      covered++;
      logger.debug(`证据点已覆盖: ${pointText.substring(0, 50)}...`);
    } else {
      missing.push(pointText.substring(0, 100));
      logger.debug(`证据点未覆盖: ${pointText.substring(0, 50)}...`);
    }
  }

  const total = evidencePoints.length;
  const coverage = total > 0 ? covered / total : 1.0;
  const coveragePercent = (coverage * 100).toFixed(1);

  logger.info(`证据覆盖率: ${coveragePercent}% (${covered}/${total})`);

  if (coverage < threshold) {
    logger.warn(`覆盖率低于阈值 ${threshold * 100}%，缺少 ${missing.length} 个证据点`);
  }

  return { coverage, covered, total, missing };
}

/**
 * 从文本中提取关键词（简化版）
 */
function extractKeywords(text) {
  // 移除常见停用词和标点
  const stopWords = new Set(['的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一', '一个', '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好', '自己', '这']);
  
  return text
    .replace(/[，。！？、；：""''（）《》\[\]{}]/g, ' ')
    .split(/\s+/)
    .filter(w => w.length >= 2 && !stopWords.has(w))
    .slice(0, 10);  // 最多取10个关键词
}

// ============================================================================
// Agent Loop 实现
// ============================================================================

/**
 * 创建法律专用 Agent Loop
 *
 * @param {object} config - 插件配置
 * @param {object} logger - 日志对象
 * @returns {object} Agent Loop 实例
 */
function createLegalAgentLoop(config, logger) {
  const preset = config.preset || DEFAULT_PRESET;
  const presetConfig = PRESETS[preset] || PRESETS.standard;

  // 合并配置
  const extraSteps = config.extraSteps || [];
  const citationStep = extraSteps.find(s => s.name === 'citation-verification');
  const coverageStep = extraSteps.find(s => s.name === 'evidence-coverage');

  const enableCitation = citationStep?.enabled !== false && presetConfig.citationVerification;
  const enableCoverage = coverageStep?.enabled !== false && presetConfig.evidenceCoverage;
  const coverageThreshold = coverageStep?.coverageThreshold || presetConfig.coverageThreshold;
  const maxRetries = Math.max(
    citationStep?.maxRetries || 2,
    coverageStep?.maxRetries || 1
  );

  logger.info(`创建 Agent Loop: preset=${preset}, citation=${enableCitation}, coverage=${enableCoverage}, threshold=${coverageThreshold}`);

  return {
    name: 'legal-agent-loop',
    preset,

    /**
     * 自定义 Loop 步骤
     * 在 DSH 默认 Loop 的 re-plan 阶段插入额外步骤
     */
    async step(context, defaultStep) {
      // 先执行默认步骤
      const result = await defaultStep(context);

      // 如果是最终回答阶段，执行额外检查
      if (result.phase === 'answer' || result.phase === 'done') {
        const response = result.response || context.response || '';
        const searchResults = context.searchResults || [];
        const evidencePoints = context.evidencePoints || [];

        // 法条引用核对
        if (enableCitation && response) {
          const citationResult = verifyCitations(response, searchResults, logger);
          result.citationVerification = citationResult;

          if (citationResult.mismatched > 0) {
            const onFailure = citationStep?.onFailure || 'warn';
            if (onFailure === 'block' && result.retries < maxRetries) {
              logger.warn(`法条引用核对失败，触发重试 (${result.retries + 1}/${maxRetries})`);
              result.shouldRetry = true;
              result.retryReason = `发现 ${citationResult.mismatched} 个法条引用未在检索结果中找到`;
            }
          }
        }

        // 证据覆盖率检查
        if (enableCoverage && response && evidencePoints.length > 0) {
          const coverageResult = checkEvidenceCoverage(response, evidencePoints, coverageThreshold, logger);
          result.evidenceCoverage = coverageResult;

          if (coverageResult.coverage < coverageThreshold) {
            const onFailure = coverageStep?.onFailure || 'warn';
            if (onFailure === 'block' && result.retries < maxRetries) {
              logger.warn(`证据覆盖率不足 (${(coverageResult.coverage * 100).toFixed(1)}%)，触发重试`);
              result.shouldRetry = true;
              result.retryReason = `证据覆盖率 ${(coverageResult.coverage * 100).toFixed(1)}% 低于阈值 ${coverageThreshold * 100}%`;
            }
          }
        }
      }

      return result;
    },

    /**
     * _internal — 内部函数导出供单元测试使用
     */
    _internal: {
      extractCitations,
      verifyCitations,
      checkEvidenceCoverage,
      extractKeywords,
      presetConfig,
    },
  };
}

// ============================================================================
// Cordis 插件入口
// ============================================================================

const name = 'agent-loop-legal';
const inject = ['agent'];

/**
 * 插件 apply 函数 — 注册自定义 Agent Loop
 *
 * @param {object} ctx — Cordis Context
 */
async function apply(ctx) {
  const pluginConfig = (ctx.config && ctx.config['agent-loop-legal']) || {};
  const logLevel = pluginConfig.logLevel || DEFAULT_LOG_LEVEL;
  const logger = createLogger(logLevel);

  logger.info('插件初始化');

  // 创建法律专用 Agent Loop
  const loop = createLegalAgentLoop(pluginConfig, logger);

  // 注册到 Cordis
  if (ctx.agent && typeof ctx.agent.setLoop === 'function') {
    ctx.agent.setLoop(loop);
    logger.info(`Agent Loop 已注册: ctx.agent.setLoop('${loop.name}')`);
  } else if (ctx.agent) {
    // 尝试直接赋值
    ctx.agent.loop = loop;
    logger.info(`Agent Loop 已注册: ctx.agent.loop = '${loop.name}'`);
  } else {
    logger.warn('ctx.agent 不可用 — 当前 DSH 版本可能不支持自定义 Agent Loop');
    logger.warn('降级模式：法律专用步骤已定义但无法注册到 DSH 运行时');
  }

  // 注册辅助函数供其他插件使用
  if (ctx.provide && typeof ctx.provide === 'function') {
    ctx.provide('agent-loop-legal:verifyCitations', verifyCitations);
    ctx.provide('agent-loop-legal:checkCoverage', checkEvidenceCoverage);
    logger.info('辅助函数已注册: verifyCitations, checkCoverage');
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
  _internal: {
    createLegalAgentLoop,
    createLogger,
    extractCitations,
    verifyCitations,
    checkEvidenceCoverage,
    extractKeywords,
    PRESETS,
    CITATION_PATTERNS,
    DEFAULT_PRESET,
    DEFAULT_LOG_LEVEL,
  },
};
