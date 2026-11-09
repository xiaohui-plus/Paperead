/**
 * A2 smoke: drive the Python proxy exactly like the browser will.
 *
 *   node agent/test/proxy-client.mjs
 */
const base = process.env.PROXY_BASE || 'http://127.0.0.1:8021/api/research'
const sessionId = process.argv[2] || `proxy-${Date.now()}`

const create = await fetch(`${base}/sessions`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ sessionId, cwd: '' }),
})
console.log('create', create.status, await create.text())

const events = await fetch(`${base}/sessions/${sessionId}/events?after=0`)
console.log('sse', events.status, events.headers.get('content-type'))
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
      buffer = frames.pop() || ''
      for (const frame of frames) {
        const data = frame.match(/^data: (.+)$/m)?.[1]
        if (!data) continue
        const event = JSON.parse(data)
        console.log(event.seq, event.type, JSON.stringify(event).slice(0, 200))
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

const sent = await fetch(`${base}/sessions/${sessionId}/messages`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ text: '请回声验证工具链路' }),
})
console.log('message', sent.status, await sent.text())

console.log('result', await done)
process.exit(0)
