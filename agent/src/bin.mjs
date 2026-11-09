/**
 * Paperead research agent entry point.
 *
 * Default: full composition — dsh-base + @paperead/dsh-research patches on an
 * empty root config, which brings skills, todo and compaction into the agent.
 * PAPEREAD_AGENT_COMPOSE=minimal: A0 minimal composition (`minimal.yml`), kept
 * for transport-level smoke tests only.
 *
 * The profile path (`dsh --profile`) is avoided on Node 24/Windows: it heals
 * per-package junctions under `$DSH_HOME/profiles/node_modules`, and Node 24
 * reports those junctions as directories.
 */
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { boot, loadOptionalPatches } from '@deepseek-ai/dsh-app-boot'

const here = dirname(fileURLToPath(import.meta.url))
const agentDir = join(here, '..')

// 兼容旧版环境变量：0.1.0 及更早的 .env / 启动脚本用的是 PAPER_REFLOW_*。
for (const [key, value] of Object.entries(process.env)) {
  if (!key.startsWith('PAPER_REFLOW_') || value === undefined) continue
  const next = `PAPEREAD_${key.slice('PAPER_REFLOW_'.length)}`
  if (process.env[next] === undefined) process.env[next] = value
}

// 内置技能目录随 agent 一起分发；显式注入，避免依赖启动时的 cwd。
process.env.PAPEREAD_SKILLS_DIR ||= join(agentDir, 'skills')

const full = process.env.PAPEREAD_AGENT_COMPOSE !== 'minimal'

let ctx
if (full) {
  const basePatch = loadOptionalPatches(
    'paperead-agent',
    join(agentDir, 'node_modules', '@deepseek-ai', 'dsh-base', 'cordis.patch.yml'),
  )
  const researchPatch = loadOptionalPatches(
    'paperead-agent',
    join(agentDir, 'bundle', 'cordis.patch.yml'),
  )
  if (!basePatch || !researchPatch) throw new Error('missing DSH bundle patch')
  console.log('[paperead-agent] booting full composition')
  ctx = await boot('paperead-agent', join(agentDir, 'cordis.yml'), [
    ...basePatch,
    ...researchPatch,
  ])
} else {
  console.log('[paperead-agent] booting minimal composition')
  ctx = await boot('paperead-agent', join(agentDir, 'minimal.yml'), [])
}
console.log('[paperead-agent] boot returned')

const shutdown = async (code) => {
  try {
    await ctx.fiber.dispose()
  } finally {
    process.exit(code)
  }
}
process.on('SIGINT', () => void shutdown(0))
process.on('SIGTERM', () => void shutdown(0))

console.log(`[paperead-agent] ready pid=${process.pid}`)
