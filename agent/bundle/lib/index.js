/**
 * Paperead research host.
 *
 * A deliberately small first slice: one HTTP/SSE surface over DSH sessions and
 * one echo tool. Later phases add the project brief, real tools and the Python
 * proxy; the wire shape here is already the one the React client consumes.
 */
import { createUserMessage } from '@deepseek-ai/dsh-llm'

export const name = 'research-host'
export const inject = ['webServer', 'agents']

const MAX_BODY_BYTES = 1_000_000
const MAX_HISTORY = 2_000

function json(res, status, value) {
  const body = JSON.stringify(value)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'access-control-allow-origin': '*',
  })
  res.end(body)
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new Error('request body too large'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      if (!chunks.length) return resolve({})
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')))
      } catch {
        reject(new Error('invalid JSON body'))
      }
    })
    req.on('error', reject)
  })
}

function sseWrite(res, event) {
  res.write(`id: ${event.seq}\n`)
  res.write(`data: ${JSON.stringify(event)}\n\n`)
}

function safeJson(text) {
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

export function apply(ctx, config = {}) {
  const token = config.token || ''
  const defaultModel = config.defaultModel || 'deepseek-v4-flash'
  const agents = new Map()
  const streams = new Map()
  const history = new Map()

  const authorized = (req) => {
    if (!token) return true
    const bearer = req.headers.authorization?.replace(/^Bearer\s+/i, '')
    return req.headers['x-agent-token'] === token || bearer === token
  }

  const emit = (sessionId, event) => {
    const events = history.get(sessionId) ?? []
    const seq = (events.at(-1)?.seq ?? 0) + 1
    const framed = { seq, at: Date.now(), ...event }
    events.push(framed)
    if (events.length > MAX_HISTORY) events.splice(0, events.length - MAX_HISTORY)
    history.set(sessionId, events)
    for (const res of streams.get(sessionId) ?? []) sseWrite(res, framed)
    return framed
  }

  // DSH publishes the same durable event stream the session log records. The
  // mapping below is intentionally lossless enough for the spike UI and becomes
  // the canonical projection in A4.
  ctx.on('session/event', (session, event) => {
    const sid = session.id
    switch (event.type) {
      case 'turn/start':
        emit(sid, { type: 'turn.start', turn: event.data.turn })
        break
      case 'turn/end':
        emit(sid, {
          type: 'turn.end',
          turn: event.data.turn,
          reason: event.data.reason,
        })
        break
      case 'assistant/chunk': {
        const chunk = event.data.chunk
        if (chunk.type === 'text-delta' && chunk.text) {
          emit(sid, { type: 'text.delta', turn: event.data.turn, step: event.data.step, text: chunk.text })
        } else if (chunk.type === 'reasoning-delta' && chunk.text) {
          emit(sid, { type: 'reasoning.delta', turn: event.data.turn, step: event.data.step, text: chunk.text })
        } else if (chunk.type === 'tool-call-delta') {
          emit(sid, {
            type: 'tool.draft',
            turn: event.data.turn,
            step: event.data.step,
            callId: chunk.id,
            name: chunk.name,
            argumentsDelta: chunk.argumentsDelta,
          })
        }
        break
      }
      case 'tool/call':
        emit(sid, {
          type: 'tool.start',
          turn: event.data.turn,
          step: event.data.step,
          callId: event.data.callId,
          name: event.data.name,
          input: safeJson(event.data.arguments) ?? event.data.arguments,
        })
        break
      case 'tool/result': {
        const message = event.data.message
        const first = message?.content?.[0]
        emit(sid, {
          type: 'tool.end',
          turn: event.data.turn,
          step: event.data.step,
          callId: first?.toolCallId,
          isError: Boolean(event.data.error) || first?.isError === true,
          error: event.data.error,
          meta: event.data.meta,
        })
        break
      }
      case 'todo/write':
        emit(sid, { type: 'plan.update', items: event.data.todos })
        break
      case 'session/title':
        emit(sid, { type: 'title.update', title: event.data.title })
        break
      default:
        break
    }
  })

  async function ensureAgent(sessionId, cwd) {
    const existing = ctx.agents.get(sessionId)
    if (existing) return existing
    if (agents.has(sessionId)) return agents.get(sessionId)

    const pending = (async () => {
      // A resumed process has the session on disk but no live agent; try the
      // persistence path first, and fall back to a fresh session on first use.
      try {
        const handle = await ctx.agents.resume({
          resumeSessionId: sessionId,
          agentOptions: {
            provider: 'deepseek-official',
            model: defaultModel,
          },
        })
        agents.set(sessionId, handle.agent)
        return handle.agent
      } catch {
        const handle = await ctx.agents.create({
          sessionId,
          meta: { cwd: cwd || process.cwd() },
          agentOptions: {
            provider: 'deepseek-official',
            model: defaultModel,
          },
        })
        agents.set(sessionId, handle.agent)
        return handle.agent
      }
    })().catch((error) => {
      agents.delete(sessionId)
      throw error
    })

    agents.set(sessionId, pending)
    return pending
  }

  ctx.webServer.register({
    kind: 'prefix',
    path: '/v1',
    handler: async (req, res) => {
      const url = new URL(req.url || '/', 'http://127.0.0.1')
      const pathname = url.pathname

      if (req.method === 'OPTIONS') {
        res.writeHead(204, {
          'access-control-allow-origin': '*',
          'access-control-allow-methods': 'GET,POST,OPTIONS',
          'access-control-allow-headers': 'content-type,authorization,x-agent-token',
        })
        return res.end()
      }

      if (pathname === '/v1/health' && req.method === 'GET') {
        return json(res, 200, {
          ok: true,
          pid: process.pid,
          sessions: ctx.agents.roots().length,
          version: '0.0.0-a0',
        })
      }

      if (!authorized(req)) return json(res, 401, { error: 'unauthorized' })

      if (pathname === '/v1/sessions' && req.method === 'POST') {
        try {
          const body = await readJson(req)
          if (!body.sessionId) return json(res, 400, { error: 'sessionId is required' })
          const agent = await ensureAgent(body.sessionId, body.cwd)
          return json(res, 200, { sessionId: agent.id, cwd: body.cwd || process.cwd() })
        } catch (error) {
          return json(res, 500, { error: String(error?.message || error) })
        }
      }

      if (pathname === '/v1/sessions' && req.method === 'GET') {
        return json(res, 200, {
          sessions: ctx.agents.roots().map((agent) => ({ sessionId: agent.id, status: agent.status, title: agent.session?.header?.id })),
        })
      }

      const messageMatch = pathname.match(/^\/v1\/sessions\/([^/]+)\/messages$/)
      if (messageMatch && req.method === 'POST') {
        const sessionId = decodeURIComponent(messageMatch[1])
        try {
          const body = await readJson(req)
          if (typeof body.text !== 'string' || !body.text.trim()) {
            return json(res, 400, { error: 'text is required' })
          }
          const agent = await ensureAgent(sessionId, body.cwd)
          agent.followup(
            createUserMessage({
              content: [{ type: 'text', text: body.text }],
              source: { kind: 'user' },
            }),
          )
          return json(res, 202, { sessionId, accepted: true })
        } catch (error) {
          return json(res, 500, { error: String(error?.message || error) })
        }
      }

      const cancelMatch = pathname.match(/^\/v1\/sessions\/([^/]+)\/cancel$/)
      if (cancelMatch && req.method === 'POST') {
        const sessionId = decodeURIComponent(cancelMatch[1])
        const agent = ctx.agents.get(sessionId)
        if (!agent) return json(res, 404, { error: 'session not found' })
        agent.cancel({ kind: 'user' })
        return json(res, 202, { sessionId, cancelled: true })
      }

      const eventsMatch = pathname.match(/^\/v1\/sessions\/([^/]+)\/events$/)
      if (eventsMatch && req.method === 'GET') {
        const sessionId = decodeURIComponent(eventsMatch[1])
        const after = Number(url.searchParams.get('after') || 0)
        res.writeHead(200, {
          'content-type': 'text/event-stream; charset=utf-8',
          'cache-control': 'no-cache, no-transform',
          connection: 'keep-alive',
          'x-accel-buffering': 'no',
          'access-control-allow-origin': '*',
        })
        res.write('retry: 1000\n\n')

        const replay = (history.get(sessionId) ?? []).filter((event) => event.seq > after)
        for (const event of replay) sseWrite(res, event)

        const set = streams.get(sessionId) ?? new Set()
        set.add(res)
        streams.set(sessionId, set)
        const timer = setInterval(() => res.write(`: ping ${Date.now()}\n\n`), 15_000)
        const cleanup = () => {
          clearInterval(timer)
          set.delete(res)
          if (!set.size) streams.delete(sessionId)
        }
        req.on('close', cleanup)
        res.on('close', cleanup)
        return
      }

      return json(res, 404, { error: 'not found' })
    },
  })
}
