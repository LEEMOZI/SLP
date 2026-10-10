import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'skill-provider-legal'

export const inject = { required: [], optional: ['skills'] }

export function apply(ctx) {
  // stderr 直写：Desktop 日志窗口只稳定透传 [stderr] 行（如 npm warn），
  // ctx.logger 是否透传不确定，故用 stderr 做加载诊断。
  process.stderr.write(`[skill-provider] apply() entered, node=${process.version}\n`)
  const skillsDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../skills')
  const logger = ctx.logger?.info ? ctx.logger : console

  if (!existsSync(skillsDir)) {
    process.stderr.write(`[skill-provider] ERROR skills dir missing: ${skillsDir}\n`)
    logger.warn?.('[skill-provider] skills dir missing:', skillsDir)
    return
  }

  const skills = []
  for (const file of readdirSync(skillsDir).filter((f) => f.endsWith('.json'))) {
    try {
      const raw = JSON.parse(readFileSync(path.join(skillsDir, file), 'utf8'))
      if (raw?.skill_id) skills.push(raw)
      else logger.warn?.('[skill-provider] skip file without skill_id:', file)
    } catch (err) {
      logger.warn?.(`[skill-provider] invalid skill file ${file}:`, err?.message)
    }
  }

  const skillsSvc = ctx.skills
  if (typeof skillsSvc?.registerProvider === 'function') {
    skillsSvc.registerProvider({
      id: 'law-agent-skill-provider',
      describe: () => skills.map((s) => ({ id: s.skill_id, name: s.skill_name })),
      provide: (id) => skills.find((s) => s.skill_id === id),
    })
    process.stderr.write(`[skill-provider] provider registered with ${skills.length} legal skills\n`)
    logger.info?.(`[skill-provider] provider registered with ${skills.length} legal skills`)
  } else {
    process.stderr.write(
      `[skill-provider] ctx.skills.registerProvider unavailable; ${skills.length} skills remain file-only\n`
    )
    logger.warn?.(
      `[skill-provider] ctx.skills.registerProvider unavailable; ${skills.length} skills remain file-only`
    )
  }
}
