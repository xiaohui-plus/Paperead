/**
 * paper-reflow research agent entry point.
 *
 * A0 boots a deliberately small composition (`minimal.yml`). The production
 * composition layers the `@deepseek-ai/dsh-base` bundle plus the
 * `@paper-reflow/dsh-research` patch; that path goes through the profile
 * loader, which heals per-package junctions under
 * `$DSH_HOME/profiles/node_modules`. Node 24 on Windows reports those
 * junctions as directories, so A0 uses the direct `boot()` path instead and
 * keeps the same bundle patches for the later slice.
 */
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { boot } from '@deepseek-ai/dsh-app-boot'

const here = dirname(fileURLToPath(import.meta.url))
const agentDir = join(here, '..')
const configPath = join(agentDir, 'minimal.yml')

console.log('[paper-reflow-agent] booting', configPath)
const ctx = await boot('paper-reflow-agent', configPath, [])
console.log('[paper-reflow-agent] boot returned')

const shutdown = async (code) => {
  try {
    await ctx.fiber.dispose()
  } finally {
    process.exit(code)
  }
}
process.on('SIGINT', () => void shutdown(0))
process.on('SIGTERM', () => void shutdown(0))

console.log(`[paper-reflow-agent] ready pid=${process.pid}`)
