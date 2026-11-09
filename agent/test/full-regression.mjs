/**
 * P1: full-composition regression against a stub LLM.
 *
 * Covers the parts of the production shape that A0 never exercised:
 *   - the real dsh-base tool inventory (todo_write present, shell/fs/subagent absent)
 *   - reasoning / tool draft / tool start / tool end / text / turn end ordering
 *   - todo/write -> plan.update
 *   - tool/result.meta survives the projection (echo presentationMeta)
 *   - session title through the LLM provider (not the fallback)
 *   - process restart -> resume -> turn counter continues
 *
 * Compaction is loaded but not triggered: reaching 80% of a real context window
 * would require a fake token budget the adapter does not expose. That row stays
 * covered by the upgrade checklist in docs/dsh-integration-notes.md.
 *
 * Run: node test/full-regression.mjs
 */
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const agentDir = dirname(dirname(fileURLToPath(import.meta.url)))
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const failures = []
function check(ok, label, detail) {
  const line = ok ? `  PASS  ${label}` : `  FAIL  ${label}${detail === undefined ? '' : ` — ${detail}`}`
  console.log(line)
  if (!ok) failures.push(label)
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

function startChild(label, script, env) {
  const child = spawn(process.execPath, [script], {
    cwd: agentDir,
    env: { ...process.env, ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const log = []
  const relay = (stream, name) => {
    stream.setEncoding('utf8')
    stream.on('data', (text) => {
      log.push(text)
      for (const line of text.split('\n')) {
        if (line.trim()) console.log(`[${label}:${name}] ${line}`)
      }
    })
  }
  relay(child.stdout, 'out')
  relay(child.stderr, 'err')
  child.on('exit', (code) => log.push(`[exit ${code}]`))
  return { child, log }
}

async function fetchJson(url, init = {}) {
  const res = await fetch(url, {
    ...init,
    headers: { 'content-type': 'application/json', ...(init.headers ?? {}) },
  })
  const text = await res.text()
  let body
  try {
    body = JSON.parse(text)
  } catch {
    body = text
  }
  if (!res.ok) throw new Error(`${init.method ?? 'GET'} ${url} -> ${res.status} ${String(text).slice(0, 400)}`)
  return body
}

async function waitFor(label, predicate, timeoutMs = 90_000, intervalMs = 300) {
  const deadline = Date.now() + timeoutMs
  let lastError
  while (Date.now() < deadline) {
    try {
      if (await predicate()) return
    } catch (error) {
      lastError = error
    }
    await sleep(intervalMs)
  }
  throw new Error(`${label} timed out after ${timeoutMs}ms${lastError ? `: ${lastError.message}` : ''}`)
}

/**
 * Open the SSE feed, then run `onOpen` so the message POST lands after the
 * subscription exists. Resolves after turn.end plus a grace window, which is
 * where the title LLM call shows up.
 */
async function collectTurn(base, sessionId, token, text, { graceMs = 6_000, timeoutMs = 120_000 } = {}) {
  const controller = new AbortController()
  const res = await fetch(`${base}/v1/sessions/${encodeURIComponent(sessionId)}/events?after=0`, {
    signal: controller.signal,
    headers: { accept: 'text/event-stream', 'x-agent-token': token },
  })
  if (!res.ok) throw new Error(`SSE ${res.status}`)

  const events = []
  let settle
  const finished = new Promise((resolve) => {
    settle = resolve
  })
  let graceTimer
  const finish = () => {
    clearTimeout(graceTimer)
    clearTimeout(hardTimer)
    controller.abort()
    settle(events)
  }
  const hardTimer = setTimeout(finish, timeoutMs)

  ;(async () => {
    const decoder = new TextDecoder()
    let buffer = ''
    try {
      for await (const chunk of res.body) {
        buffer += decoder.decode(chunk, { stream: true })
        let boundary = buffer.indexOf('\n\n')
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          for (const line of frame.split('\n')) {
            if (!line.startsWith('data: ')) continue
            let event
            try {
              event = JSON.parse(line.slice(6))
            } catch {
              continue
            }
            events.push(event)
            if (event.type === 'turn.end') clearTimeout(graceTimer)
            if (event.type === 'turn.end') graceTimer = setTimeout(finish, graceMs)
          }
          boundary = buffer.indexOf('\n\n')
        }
      }
    } catch {
      // Abort after settle is expected.
    }
    finish()
  })()

  await fetchJson(`${base}/v1/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    headers: { 'x-agent-token': token },
    body: JSON.stringify({ text }),
  })
  return finished
}

function firstIndex(events, type) {
  return events.findIndex((event) => event.type === type)
}

const root = await mkdtemp(join(tmpdir(), 'paperead-regress-'))
const recordPath = join(root, 'requests.json')
const dataHome = join(root, 'dsh-home')
const token = 'regress-token'
const sessionId = `regress-${Date.now()}`

let mock
let agent
let base
const env = {}

try {
  const mockPort = await freePort()
  mock = startChild('mock', join(agentDir, 'test', 'mock-llm.mjs'), {
    MOCK_LLM_PORT: String(mockPort),
    MOCK_LLM_RECORD: recordPath,
  })
  await waitFor('mock-llm', async () => {
    const res = await fetch(`http://127.0.0.1:${mockPort}/__record`)
    return res.ok
  }, 15_000)

  Object.assign(env, {
    DSH_HOME: dataHome,
    PAPEREAD_AGENT_COMPOSE: 'full',
    PAPEREAD_AGENT_TOKEN: token,
    PAPEREAD_LLM_BASE: `http://127.0.0.1:${mockPort}/v1`,
    PAPEREAD_LLM_KEY: 'test-key',
    PAPEREAD_MODEL: 'deepseek-v4-flash',
  })

  let agentPort = await freePort()
  base = `http://127.0.0.1:${agentPort}`
  agent = startChild('agent', join(agentDir, 'src', 'bin.mjs'), {
    ...env,
    PAPEREAD_AGENT_PORT: String(agentPort),
  })
  await waitFor('agent health', async () => {
    const res = await fetch(`${base}/v1/health`)
    return res.ok
  })
  console.log('\n[1] full composition booted\n')

  // ---- turn 1: tools, todo, title, meta -------------------------------
  const first = await collectTurn(base, sessionId, token, '请先列出研究计划，然后回声确认。')
  const types = first.map((event) => event.type)
  check(types.includes('turn.start') && types.includes('turn.end'), 'turn envelope', types.join(','))
  check(
    firstIndex(first, 'turn.start') < firstIndex(first, 'tool.draft') &&
      firstIndex(first, 'tool.draft') < firstIndex(first, 'tool.start') &&
      firstIndex(first, 'tool.start') < firstIndex(first, 'tool.end') &&
      firstIndex(first, 'tool.end') < firstIndex(first, 'turn.end'),
    'chunk/tool/turn ordering',
    types.join(','),
  )

  const turnEnd = first.findLast((event) => event.type === 'turn.end')
  check(turnEnd?.reason?.kind === 'completed', 'turn.end.reason.kind = completed', JSON.stringify(turnEnd?.reason))

  const plan = first.find((event) => event.type === 'plan.update')
  check(
    Array.isArray(plan?.items) && plan.items.length === 1 && plan.items[0].status === 'in_progress',
    'todo/write -> plan.update',
    JSON.stringify(plan?.items),
  )

  const ends = first.filter((event) => event.type === 'tool.end')
  const names = ends.map((event) => event.name)
  check(ends.length >= 2, 'two tool results projected', String(ends.length))
  check(names.includes('todo_write') && names.includes('echo'), 'tool.end carries names', JSON.stringify(names))
  check(ends.every((event) => typeof event.callId === 'string' && event.callId), 'tool.end carries callId', JSON.stringify(ends.map((event) => event.callId)))
  const echoEnd = ends.find((event) => event.name === 'echo')
  check(echoEnd?.meta?.kind === 'echo' && echoEnd.meta.text === 'pong', 'presentationMeta survives in tool/result.meta', JSON.stringify(echoEnd?.meta))

  const title = first.findLast((event) => event.type === 'title.update')
  check(title?.source === 'provider' && typeof title.title === 'string' && title.title.length > 0, 'LLM title (source=provider)', JSON.stringify(title))

  const sessions = await fetchJson(`${base}/v1/sessions`, { headers: { 'x-agent-token': token } })
  check(sessions.sessions?.[0]?.title === title?.title, 'GET /v1/sessions returns last title', JSON.stringify(sessions.sessions))

  // ---- tool inventory from the stub's request log ---------------------
  const records = JSON.parse(await readFile(recordPath, 'utf8'))
  const mainRequests = records.filter((entry) => entry.kind === 'main')
  const inventory = [...new Set(mainRequests.flatMap((entry) => entry.tools))]
  check(inventory.includes('todo_write') && inventory.includes('echo'), 'main request exposes todo_write + echo', inventory.join(','))
  check(
    mainRequests.some((entry) => entry.mentionsLatexSkill),
    'built-in write-latex skill reaches the model request',
    JSON.stringify(mainRequests.map((entry) => entry.mentionsLatexSkill)),
  )
  const banned = inventory.filter((name) =>
    /^(bash|pwsh|str_replace_editor|job_|subagent|workflow_|ralph|goal_|exit_plan_mode|read|write|edit|glob|grep|ls)/.test(name),
  )
  check(banned.length === 0, 'shell/fs/subagent tools stay disabled', banned.join(','))
  check(records.some((entry) => entry.kind === 'title'), 'title request reached the provider', String(records.length))

  // ---- restart + resume ------------------------------------------------
  console.log('\n[2] restart and resume\n')
  agent.child.kill()
  await new Promise((resolve) => agent.child.once('exit', resolve))
  agentPort = await freePort()
  base = `http://127.0.0.1:${agentPort}`
  agent = startChild('agent2', join(agentDir, 'src', 'bin.mjs'), {
    ...env,
    PAPEREAD_AGENT_PORT: String(agentPort),
  })
  await waitFor('agent health (restart)', async () => {
    const res = await fetch(`${base}/v1/health`)
    return res.ok
  })

  await fetchJson(`${base}/v1/sessions`, {
    method: 'POST',
    headers: { 'x-agent-token': token },
    body: JSON.stringify({ sessionId }),
  })
  const second = await collectTurn(base, sessionId, token, '继续。')
  const secondEnd = second.findLast((event) => event.type === 'turn.end')
  check(secondEnd?.turn === 2, 'resume continues at turn 2', JSON.stringify(secondEnd))
  check(second.some((event) => event.type === 'text.delta'), 'resumed turn streams text', second.map((event) => event.type).join(','))
} catch (error) {
  failures.push(error.message)
  console.error(`\nFATAL ${error.stack || error.message}`)
  if (agent) console.error(`--- agent log tail ---\n${agent.log.join('').slice(-4000)}`)
  if (mock) console.error(`--- mock log tail ---\n${mock.log.join('').slice(-2000)}`)
} finally {
  agent?.child.kill()
  mock?.child.kill()
  await sleep(500)
  await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }).catch(() => {})
}

console.log(failures.length ? `\nFAILED (${failures.length}): ${failures.join(' | ')}` : '\nALL CHECKS PASSED')
process.exit(failures.length ? 1 : 0)
