/**
 * @law-agent/policy-legal-safety-bundle — TypeScript 骨架
 *
 * 这是 P0 阶段提供的"最小可运行模板"。当前版本**未实现全部逻辑**，只做两件事：
 *   1. 导出 name / inject / apply，让 Cordis 能加载；
 *   2. 在 apply 里注册一个"空透传"的 tools/pre-execute hook，把决策交给下一层（也就是 cordis.patch.yml 的配置层）。
 *
 * 等 DSH 正式装好，用 `npx tsc --build` 编译本文件为 dist/safety.js，然后即可进入正式开发。
 * 编译前请先在本目录执行 `npm install` 以拉取 peer 依赖的类型声明。
 *
 * 后续实现要点详见 README.md 的"拦截机制"一节。
 *
 * @ts-nocheck  — P0 作为骨架模板，未安装 peer 依赖（@deepseek-ai/cordis、@deepseek-ai/dsh-tools）。
 *                正式开发时移除该行、执行 npm install 后，IDE 即可做类型校验。
 */
// @ts-nocheck
import type { Context } from '@deepseek-ai/cordis'
import type { PreToolDecision, ToolExecution } from '@deepseek-ai/dsh-tools'

export const name = 'policy-legal-safety'

export const inject = ['tools'] as const

export async function apply(ctx: Context): Promise<void> {
  ctx.on('tools/pre-execute', async (exec: ToolExecution, next): Promise<PreToolDecision> => {
    // P0 骨架：直接透传。正式实现会在这里做 3 层拦截（白名单 / 路径越权 / 高危关键词）。
    return next()
  })
}
