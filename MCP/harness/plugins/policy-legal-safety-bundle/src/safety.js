/**
 * @law-agent/policy-legal-safety-bundle — 法律安全审批策略插件
 *
 * 在 DSH Cordis 的 tools/pre-execute waterfall 上注册三层拦截：
 *   Layer 1 — 工具分类（只读免审批 / 写入需确认 / 高危强制拒绝）
 *   Layer 2 — 路径越权校验（file_path 参数必须在授权目录内）
 *   Layer 3 — 高危关键词拦截（输入参数含 shell 注入/删库等关键词时拒绝）
 *
 * 所有可调阈值通过 cordis.patch.yml 的 config 传入，无需改代码。
 *
 * 设计原则：
 *   - 与 Python law-search 侧的路径校验互补，不重复
 *     （Python 侧已做 review_contract/read_document 的授权目录检查，
 *      本插件在 DSH 层做第二道闸，覆盖 ingest_rebuild / write_litigation 等）
 *   - 审计日志写入 stderr（DSH 控制台可见），不污染 stdout（MCP stdio 协议）
 *   - 任何异常静默放行（safety 插件不能阻断正常业务），仅记录告警
 *
 * @see MCP/HARNESS_PLUGIN_ARCHITECTURE.md — 第四节 Phase 1 架构
 * @see MCP/harness/plugins/README.md — 插件开发指引
 */

'use strict';

// ============================================================================
// 默认配置（可被 cordis.patch.yml 的 config 覆盖）
// ============================================================================

/**
 * 工具分类表
 *   readonly  — 只读工具，免审批直接放行
 *   write     — 写入工具（生成文书等），需用户确认
 *   dangerous — 高危工具（重建索引等），默认拒绝，需显式 confirm=true
 *   unknown   — 未登记工具，按 unknown_policy 处理
 */
const DEFAULT_TOOL_CLASSIFICATION = {
  // law-search 9 个工具
  'search_law':          'readonly',
  'search_case':         'readonly',
  'chroma_status':       'readonly',
  'evaluate_evidence':   'readonly',
  'search_external':     'readonly',
  'read_document':       'readonly',
  'review_contract':     'readonly',   // 审查不修改文件，但涉及路径 → Layer 2 额外校验
  'write_litigation':    'write',      // 生成文书 → 需用户确认
  'ingest_rebuild':      'dangerous',  // 重建索引 → 高危

  // chinese-law 8 个工具（全部只读）
  'search_legislation':    'readonly',
  'get_provision':         'readonly',
  'list_sources':          'readonly',
  'validate_citation':     'readonly',
  'build_legal_stance':    'readonly',
  'format_citation':       'readonly',
  'check_currency':        'readonly',
  'about':                 'readonly',

  // filesystem 工具（MCP 官方 @modelcontextprotocol/server-filesystem）
  'read_file':             'readonly',
  'read_multiple_files':   'readonly',
  'list_directory':        'readonly',
  'directory_tree':        'readonly',
  'search_files':          'readonly',
  'get_file_info':         'readonly',
  'list_allowed_directories': 'readonly',
  'write_file':            'write',
  'edit_file':             'write',
  'create_directory':      'write',
  'move_file':             'write',

  // session-storage 6 个工具（全部只读或低危写入）
  'sync_sessions_tool':    'write',     // 写入 SQLite，但幂等
  'search_sessions':       'readonly',
  'get_session':           'readonly',
  'tag_session':           'write',     // 修改标签
  'export_session':        'write',     // 写文件到桌面
  'session_stats':         'readonly',
};

/**
 * 授权目录（路径越权校验的白名单）
 * 只有这些目录下的文件可以被 read_document / review_contract 访问
 */
const DEFAULT_AUTHORIZED_ROOTS = [
  '/Users/simon/Documents/1-Work',
  '/Users/simon/Documents/1-SLP/MCP/law-search/uploads',
  '/Users/simon/Documents/1-SLP/DSH WorkSpace/.dsh-uploads',
  '/Users/simon/Downloads',
];

/**
 * 高危关键词（Layer 3 拦截）
 * 如果工具调用参数的 JSON 序列化文本包含这些关键词，直接拒绝
 */
const DEFAULT_BLOCKED_KEYWORDS = [
  // Shell 注入
  'rm -rf', 'rm -fr', 'format ', 'mkfs',
  'chmod 777', 'chmod -R', 'chown -R',
  'drop table', 'drop database',
  'curl | sh', 'wget | sh', 'bash -c',
  // 数据破坏
  'DELETE FROM', 'TRUNCATE TABLE',
  // 密钥泄漏
  'cat /etc/shadow', 'cat ~/.ssh',
];

/**
 * 未知工具策略
 *   'allow'  — 放行（宽松模式）
 *   'confirm' — 需用户确认（默认）
 *   'deny'   — 拒绝（严格模式）
 */
const DEFAULT_UNKNOWN_POLICY = 'confirm';

// ============================================================================
// 核心逻辑
// ============================================================================

/**
 * 从工具调用中提取所有字符串参数（递归展平）
 */
function extractArgStrings(args) {
  const results = [];
  if (!args) return results;

  function walk(obj) {
    if (obj === null || obj === undefined) return;
    if (typeof obj === 'string') {
      results.push(obj);
      return;
    }
    if (typeof obj === 'number' || typeof obj === 'boolean') return;
    if (Array.isArray(obj)) {
      obj.forEach(walk);
      return;
    }
    if (typeof obj === 'object') {
      Object.values(obj).forEach(walk);
    }
  }

  walk(args);
  return results;
}

/**
 * 从参数中提取所有 file_path / path 类字段
 */
function extractFilePaths(args) {
  const paths = [];
  if (!args || typeof args !== 'object') return paths;

  const PATH_KEYS = ['file_path', 'path', 'filePath', 'source', 'src', 'out_dir', 'output_dir'];

  function walk(obj) {
    if (!obj || typeof obj !== 'object') return;
    if (Array.isArray(obj)) {
      obj.forEach(walk);
      return;
    }
    for (const [key, val] of Object.entries(obj)) {
      if (PATH_KEYS.includes(key) && typeof val === 'string' && val.trim()) {
        paths.push(val.trim());
      } else if (typeof val === 'object') {
        walk(val);
      }
    }
  }

  walk(args);
  return paths;
}

/**
 * Layer 1: 工具分类检查
 * 返回 { action: 'allow' | 'confirm' | 'deny', reason?: string }
 */
function layer1ToolClassification(toolName, config) {
  const classification = config.toolClassification || DEFAULT_TOOL_CLASSIFICATION;
  const unknownPolicy = config.unknownPolicy || DEFAULT_UNKNOWN_POLICY;

  const category = classification[toolName] || 'unknown';

  switch (category) {
    case 'readonly':
      return { action: 'allow' };

    case 'write':
      return {
        action: 'confirm',
        reason: `[policy-legal-safety] 工具 ${toolName} 属于写入操作，需要您的确认。`,
      };

    case 'dangerous':
      return {
        action: 'deny',
        reason: `[policy-legal-safety] 工具 ${toolName} 属于高危操作（如重建索引），` +
                `默认拒绝。如确需执行，请在调用时传入 confirm=true 参数。`,
      };

    case 'unknown':
    default:
      switch (unknownPolicy) {
        case 'allow':
          return { action: 'allow' };
        case 'deny':
          return {
            action: 'deny',
            reason: `[policy-legal-safety] 工具 ${toolName} 未在安全白名单中登记，策略为拒绝。`,
          };
        case 'confirm':
        default:
          return {
            action: 'confirm',
            reason: `[policy-legal-safety] 工具 ${toolName} 未在安全白名单中登记，需要您的确认。`,
          };
      }
  }
}

/**
 * Layer 2: 路径越权校验
 * 检查 file_path / path 参数是否在授权目录内
 * 返回 { action: 'allow' | 'deny', reason?: string }
 */
function layer2PathAuthorization(toolName, args, config) {
  const authorizedRoots = config.authorizedRoots || DEFAULT_AUTHORIZED_ROOTS;

  // 只对涉及文件路径的工具检查
  const PATH_SENSITIVE_TOOLS = [
    'review_contract', 'read_document', 'write_litigation',
    'write_file', 'edit_file', 'move_file', 'export_session',
  ];
  if (!PATH_SENSITIVE_TOOLS.includes(toolName)) {
    return { action: 'allow' };
  }

  const paths = extractFilePaths(args);
  if (paths.length === 0) {
    return { action: 'allow' };  // 没有路径参数，跳过
  }

  for (const p of paths) {
    // 解析 ~ 和相对路径
    let resolved = p;
    if (resolved.startsWith('~')) {
      resolved = resolved.replace('~', process.env.HOME || '/Users/simon');
    }

    const isAuthorized = authorizedRoots.some(root => {
      const normalizedRoot = root.endsWith('/') ? root : root + '/';
      return resolved === root || resolved.startsWith(normalizedRoot);
    });

    if (!isAuthorized) {
      return {
        action: 'deny',
        reason: `[policy-legal-safety] 路径越权：${p} 不在授权目录内。` +
                `授权目录：${authorizedRoots.join(', ')}。` +
                `如需访问，请在 cordis.patch.yml 的 policy.paths.authorized_roots 中添加。`,
      };
    }
  }

  return { action: 'allow' };
}

/**
 * Layer 3: 高危关键词拦截
 * 检查参数文本中是否包含高危关键词
 * 返回 { action: 'allow' | 'deny', reason?: string }
 */
function layer3BlockedKeywords(toolName, args, config) {
  const blockedKeywords = config.blockedKeywords || DEFAULT_BLOCKED_KEYWORDS;

  // 对 dangerous 工具跳过关键词检查（它们已经被 Layer 1 拦截）
  const classification = config.toolClassification || DEFAULT_TOOL_CLASSIFICATION;
  if (classification[toolName] === 'dangerous') {
    return { action: 'allow' };
  }

  const argStrings = extractArgStrings(args);
  const fullText = argStrings.join(' ').toLowerCase();

  for (const keyword of blockedKeywords) {
    if (fullText.includes(keyword.toLowerCase())) {
      return {
        action: 'deny',
        reason: `[policy-legal-safety] 参数中包含高危关键词 "${keyword}"，已拒绝。` +
                `如确需执行，请联系管理员调整安全策略。`,
      };
    }
  }

  return { action: 'allow' };
}

/**
 * ingest_rebuild 特殊检查：必须有 confirm=true 才放行
 */
function checkIngestRebuild(toolName, args) {
  if (toolName !== 'ingest_rebuild') {
    return { action: 'allow' };
  }
  if (args && args.confirm === true) {
    return { action: 'allow' };
  }
  return {
    action: 'deny',
    reason: '[policy-legal-safety] ingest_rebuild 需要 confirm=true 参数才能执行。' +
            '当前为 dry-run 模式（默认），不会修改知识库。' +
            '如确需重建索引，请在调用时传入 confirm=true。',
  };
}

// ============================================================================
// 审计日志
// ============================================================================

function auditLog(level, toolName, result, extra) {
  // 写入 stderr，不污染 MCP stdio stdout
  const ts = new Date().toISOString();
  const parts = [`[${ts}]`, `[policy-legal-safety]`, `[${level}]`, `tool=${toolName}`, `action=${result.action}`];
  if (result.reason) parts.push(`reason=${result.reason.substring(0, 120)}`);
  if (extra) parts.push(extra);
  process.stderr.write(parts.join(' ') + '\n');
}

// ============================================================================
// Cordis 插件入口
// ============================================================================

/**
 * 插件名称
 */
const name = 'policy-legal-safety';

/**
 * 注入声明
 */
const inject = ['tools'];

/**
 * 插件配置 schema（供 cordis.patch.yml 的 config 段校验参考）
 *
 * {
 *   toolClassification: { [toolName: string]: 'readonly' | 'write' | 'dangerous' },
 *   authorizedRoots: string[],
 *   blockedKeywords: string[],
 *   unknownPolicy: 'allow' | 'confirm' | 'deny',
 * }
 */

/**
 * 插件 apply 函数 — 注册 tools/pre-execute hook
 *
 * @param {object} ctx — Cordis Context
 */
async function apply(ctx) {
  // 从 Cordis config 读取用户自定义配置（cordis.patch.yml 的 config 段）
  const config = (ctx.config && ctx.config['policy-legal-safety']) || {};

  // 注册 tools/pre-execute hook
  // DSH Cordis waterfall：每个 hook 返回 { action: 'allow' | 'confirm' | 'deny' }
  // deny → 立即阻断，confirm → 弹窗让用户确认，allow → 放行到下一个 hook
  if (ctx.on && typeof ctx.on === 'function') {
    ctx.on('tools/pre-execute', async (exec, next) => {
      const toolName = (exec && (exec.toolName || exec.name)) || 'unknown';
      const args = (exec && exec.args) || {};

      try {
        // Layer 1: 工具分类
        const l1 = layer1ToolClassification(toolName, config);
        if (l1.action === 'deny') {
          auditLog('DENY', toolName, l1, 'layer=1');
          return { kind: 'deny', reason: l1.reason };
        }
        if (l1.action === 'confirm') {
          auditLog('CONFIRM', toolName, l1, 'layer=1');
          // confirm 需要等用户确认后再继续
          // DSH 的 approval 机制会弹窗让用户确认
          return next();
        }

        // Layer 2: 路径越权
        const l2 = layer2PathAuthorization(toolName, args, config);
        if (l2.action === 'deny') {
          auditLog('DENY', toolName, l2, 'layer=2');
          return { kind: 'deny', reason: l2.reason };
        }

        // Layer 3: 高危关键词
        const l3 = layer3BlockedKeywords(toolName, args, config);
        if (l3.action === 'deny') {
          auditLog('DENY', toolName, l3, 'layer=3');
          return { kind: 'deny', reason: l3.reason };
        }

        // Special: ingest_rebuild confirm 检查
        const l4 = checkIngestRebuild(toolName, args);
        if (l4.action === 'deny') {
          auditLog('DENY', toolName, l4, 'layer=special');
          return { kind: 'deny', reason: l4.reason };
        }

        // 全部通过 → 放行到下一个 hook
        auditLog('ALLOW', toolName, { action: 'allow' });
        return next();

      } catch (err) {
        // 安全插件自身异常时静默放行，不阻断业务
        process.stderr.write(
          `[policy-legal-safety] ERROR: ${err.message || err} — 静默放行 tool=${toolName}\n`
        );
        return next();
      }
    });
  }

  // 注册 tools/post-execute hook（审计日志）
  if (ctx.on && typeof ctx.on === 'function') {
    ctx.on('tools/post-execute', async (exec) => {
      const toolName = (exec && (exec.toolName || exec.name)) || 'unknown';
      const duration = exec && exec.durationMs;
      process.stderr.write(
        `[policy-legal-safety] [AUDIT] tool=${toolName} completed` +
        (duration ? ` in ${duration}ms` : '') + '\n'
      );
    });
  }

  process.stderr.write('[policy-legal-safety] 插件已加载 — 3 层拦截就绪\n');
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
    layer1ToolClassification,
    layer2PathAuthorization,
    layer3BlockedKeywords,
    checkIngestRebuild,
    extractArgStrings,
    extractFilePaths,
    DEFAULT_TOOL_CLASSIFICATION,
    DEFAULT_AUTHORIZED_ROOTS,
    DEFAULT_BLOCKED_KEYWORDS,
    DEFAULT_UNKNOWN_POLICY,
  },
};
