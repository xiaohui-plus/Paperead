/**
 * A0 smoke client: connect SSE, send one message, print every event.
 *
 *   node agent/test/spike-client.mjs [sessionId]
 */
const base = process.env.AGENT_BASE || 'http://127.0.0.1:8011'
const token = process.env.AGENT_TOKEN || 'dev-token'
const sessionId = process.argv[2] || `spike-${Date.now()}`

const headers = { 'x-agent-token': token }

const created = await fetch(`${base}/v1/sessions`, {
  method: 'POST',
  headers: { ...headers, 'content-type': 'application/json' },
  body: JSON.stringify({ sessionId, cwd: process.cwd() }),
})
console.log('create', created.status, await created.text())

const events = await fetch(`${base}/v1/sessions/${sessionId}/events?after=0`, { headers })
const reader = events.body.getReader()
const decoder = new TextDecoder()

const done = new Promise((resolve) => {
  let buffer = ''
  const timer = setTimeout(() => resolve('timeout'), 20_000)
  ;(async () => {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const id = frame.match(/^id: (\d+)$/m)?.[1]
        const data = frame.match(/^data: (.+)$/m)?.[1]
        if (!data) continue
        const event = JSON.parse(data)
        console.log(id ?? '-', event.type, JSON.stringify(event).slice(0, 240))
        if (event.type === 'turn.end') {
          clearTimeout(timer)
          resolve('turn.end')
          return
        }
      }
    }
    clearTimeout(timer)
    resolve('closed')
  })().catch(() => resolve('error'))
})

const sent = await fetch(`${base}/v1/sessions/${sessionId}/messages`, {
  method: 'POST',
  headers: { ...headers, 'content-type': 'application/json' },
  body: JSON.stringify({ text: '请回声验证工具链路' }),
})
console.log('message', sent.status, await sent.text())

console.log('result', await done)
process.exit(0)
