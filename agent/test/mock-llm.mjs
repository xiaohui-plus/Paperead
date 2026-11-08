/**
 * Tiny DeepSeek-compatible SSE server for the A0 spike.
 *
 * First request: stream a reasoning delta + one `echo` tool call.
 * Every later request: stream a final text answer.
 */
import { createServer } from 'node:http'

const PORT = Number(process.env.MOCK_LLM_PORT || 8012)
let requests = 0

function send(res, payload) {
  res.write(`data: ${JSON.stringify(payload)}\n\n`)
}

function chunk(delta, finish = null) {
  return {
    id: 'mock-1',
    object: 'chat.completion.chunk',
    created: Math.floor(Date.now() / 1000),
    model: 'deepseek-v4-flash',
    choices: [{ index: 0, delta, finish_reason: finish }],
  }
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')))
      } catch {
        resolve({})
      }
    })
  })
}

const server = createServer(async (req, res) => {
  if (req.method !== 'POST' || !req.url.endsWith('/chat/completions')) {
    res.writeHead(404).end('not found')
    return
  }

  await readBody(req)
  const first = requests++ === 0

  res.writeHead(200, {
    'content-type': 'text/event-stream; charset=utf-8',
    'cache-control': 'no-cache',
    connection: 'keep-alive',
  })

  if (first) {
    send(res, chunk({ role: 'assistant', reasoning_content: '先回声一下，验证工具链路。' }))
    setTimeout(() => {
      send(
        res,
        chunk({
          tool_calls: [
            {
              index: 0,
              id: 'call_echo',
              type: 'function',
              function: { name: 'echo', arguments: '{"text":"pong"}' },
            },
          ],
        }),
      )
      send(res, {
        ...chunk({}, 'tool_calls'),
        usage: { prompt_tokens: 20, completion_tokens: 8, total_tokens: 28 },
      })
      res.write('data: [DONE]\n\n')
      res.end()
    }, 80)
    return
  }

  send(res, chunk({ role: 'assistant', content: '工具返回：' }))
  setTimeout(() => {
    send(res, chunk({ content: 'pong' }))
    send(res, {
      ...chunk({}, 'stop'),
      usage: { prompt_tokens: 40, completion_tokens: 5, total_tokens: 45 },
    })
    res.write('data: [DONE]\n\n')
    res.end()
  }, 60)
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[mock-llm] listening on http://127.0.0.1:${PORT}`)
})
