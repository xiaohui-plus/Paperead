/**
 * DeepSeek-compatible SSE stub used by the A0 spike and the P1 regression.
 *
 * The response is derived from the request, not from a global counter, so it
 * stays correct when the title plugin and the main conversation interleave:
 *
 *   - title request (session-title purpose, maxTokens=64, no tools) -> short title
 *   - compaction request (no tools, summarization prompt)          -> summary text
 *   - main request, last message is `user`  -> reasoning + tool calls
 *   - main request, last message is `tool`  -> final text
 *
 * Available tools decide which calls to emit: `todo_write` when present, so the
 * full composition exercises plan updates; otherwise `echo` (A0 minimal).
 *
 * MOCK_LLM_RECORD=<path> dumps every request classification as JSON for the
 * regression script to assert on (tool inventory, request kinds).
 */
import { createServer } from 'node:http'
import { writeFileSync } from 'node:fs'

const PORT = Number(process.env.MOCK_LLM_PORT || 8012)
const RECORD = process.env.MOCK_LLM_RECORD || ''
// 模拟不规范网关：只发数据块、不发 [DONE]，用来验证本地代理的补齐逻辑。
const OMIT_DONE = process.env.MOCK_LLM_OMIT_DONE === '1'
// 主对话只回文本、不要求工具，便于单独验证流式收尾（不依赖内部工具接口）。
const TEXT_ONLY = process.env.MOCK_LLM_TEXT_ONLY === '1'
// 子代理链路回归：父级请求派生子代理，子代理自己的请求继续走普通工具分支。
const SUBAGENT = process.env.MOCK_SUBAGENT === '1'
// 文件链路回归：按用户轮次依次调用 write / read / edit。
const FS = process.env.MOCK_FS === '1'
const records = []

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

function textOf(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.map((part) => (typeof part === 'string' ? part : part?.text ?? '')).join('')
}

function allText(body) {
  const parts = []
  if (typeof body.system === 'string') parts.push(body.system)
  else if (Array.isArray(body.system)) parts.push(...body.system.map((part) => part?.text ?? ''))
  for (const message of body.messages ?? []) {
    if (message?.role === 'system') parts.push(textOf(message.content))
    if (message?.role === 'user') parts.push(textOf(message.content))
  }
  return parts.join('\n')
}

function toolNames(body) {
  return (body.tools ?? []).map((tool) => tool?.function?.name ?? tool?.name).filter(Boolean)
}

function classify(body) {
  const tools = toolNames(body)
  const text = allText(body)
  if (/concise title|session title/i.test(text) || (Number(body.max_tokens) <= 64 && tools.length === 0)) {
    return { kind: 'title', tools }
  }
  if (tools.length === 0 && /compact|summar/i.test(text)) return { kind: 'compaction', tools }
  const lastRole = body.messages?.at(-1)?.role ?? 'user'
  const users = (body.messages ?? []).filter((message) => message?.role === 'user')
  const lastUser = [...users]
    .reverse()
    .find((message) => {
      const text = textOf(message.content).trimStart()
      // skill-filesystem 会把技能目录作为一条 user 注入，末尾可能覆盖真实提问。
      return text.length > 0 && !text.startsWith('<system-reminder>')
    })
  const prompt = textOf(lastUser?.content)
  return { kind: lastRole === 'tool' ? 'main-followup' : 'main', tools, lastRole, prompt }
}

function record(body, info) {
  records.push({
    kind: info.kind,
    tools: info.tools,
    lastRole: info.lastRole,
    prompt: (info.prompt || '').slice(0, 120),
    maxTokens: body.max_tokens,
    // 内置技能是否进入模型请求（回归用来确认 agent/skills 被真正扫描到）。
    mentionsLatexSkill: /write-latex/.test(allText(body)),
    at: Date.now(),
  })
  if (RECORD) {
    try {
      writeFileSync(RECORD, JSON.stringify(records, null, 2))
    } catch {
      // Recording is diagnostic only.
    }
  }
}

function endStream(res) {
  if (!OMIT_DONE) res.write('data: [DONE]\n\n')
  res.end()
}

function finishOk(res) {
  send(res, { ...chunk({}, 'stop'), usage: { prompt_tokens: 40, completion_tokens: 5, total_tokens: 45 } })
  endStream(res)
}

function streamText(res, text) {
  send(res, chunk({ role: 'assistant', content: text.slice(0, Math.max(1, text.length - 2)) }))
  setTimeout(() => {
    send(res, chunk({ content: text.slice(Math.max(1, text.length - 2)) }))
    finishOk(res)
  }, 30)
}

function streamToolCall(res, id, name, args, reasoning) {
  send(res, chunk({ role: 'assistant', reasoning_content: reasoning }))
  setTimeout(() => {
    send(
      res,
      chunk({
        tool_calls: [
          {
            index: 0,
            id,
            type: 'function',
            function: { name, arguments: JSON.stringify(args) },
          },
        ],
      }),
    )
    send(res, { ...chunk({}, 'tool_calls'), usage: { prompt_tokens: 20, completion_tokens: 8, total_tokens: 28 } })
    endStream(res)
  }, 60)
}

function streamMain(res, info) {
  const hasTodo = info.tools.includes('todo_write')
  const hasEcho = info.tools.includes('echo')
  if (FS && info.tools.includes('report') === false && info.lastRole === 'user') {
    const prompt = info.prompt || ''
    if (prompt.includes('创建') && info.tools.includes('write')) {
      return streamToolCall(res, 'call_write', 'write', { file_path: 'hello.md', content: '第一版\n' }, '写入文件验证')
    }
    if (prompt.includes('读取') && info.tools.includes('read')) {
      return streamToolCall(res, 'call_read', 'read', { file_path: 'hello.md' }, '读取文件验证')
    }
    if (prompt.includes('改成') && info.tools.includes('edit')) {
      return streamToolCall(
        res,
        'call_edit',
        'edit',
        { file_path: 'hello.md', old_string: '第一版', new_string: '第二版' },
        '编辑文件验证',
      )
    }
    return streamText(res, '文件链路验证完成。')
  }
  // 父级没有 report（它只注册在子代理作用域），据此只派生一层，避免递归。
  if (SUBAGENT && info.tools.includes('subagent') && !info.tools.includes('report')) {
    send(res, chunk({ role: 'assistant', reasoning_content: '派生一个子代理核对工具链路。' }))
    setTimeout(() => {
      send(
        res,
        chunk({
          tool_calls: [
            {
              index: 0,
              id: 'call_subagent',
              type: 'function',
              function: {
                name: 'subagent',
                arguments: JSON.stringify({
                  description: '子代理回归',
                  prompt: '请调用 echo 工具确认链路，然后总结结果。',
                }),
              },
            },
          ],
        }),
      )
      send(res, { ...chunk({}, 'tool_calls'), usage: { prompt_tokens: 20, completion_tokens: 8, total_tokens: 28 } })
      endStream(res)
    }, 60)
    return
  }
  if (!hasTodo && !hasEcho) {
    streamText(res, '（假模型）没有可调用的工具，直接回答。')
    return
  }

  send(res, chunk({ role: 'assistant', reasoning_content: '先列计划，再回声，验证工具链路。' }))
  setTimeout(() => {
    let index = 0
    if (hasTodo) {
      const args = JSON.stringify({
        todos: [{ content: '核对 DSH 接入契约', status: 'in_progress' }],
      })
      send(
        res,
        chunk({
          tool_calls: [
            { index, id: 'call_todo', type: 'function', function: { name: 'todo_write', arguments: args } },
          ],
        }),
      )
      index += 1
    }
    if (hasEcho) {
      send(
        res,
        chunk({
          tool_calls: [
            {
              index,
              id: 'call_echo',
              type: 'function',
              function: { name: 'echo', arguments: '{"text":"pong"}' },
            },
          ],
        }),
      )
    }
    send(res, { ...chunk({}, 'tool_calls'), usage: { prompt_tokens: 20, completion_tokens: 8, total_tokens: 28 } })
    endStream(res)
  }, 60)
}

const server = createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/__record') {
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(records, null, 2))
    return
  }
  if (req.method !== 'POST' || !req.url.endsWith('/chat/completions')) {
    res.writeHead(404).end('not found')
    return
  }

  const body = await readBody(req)
  const info = classify(body)
  record(body, info)

  res.writeHead(200, {
    'content-type': 'text/event-stream; charset=utf-8',
    'cache-control': 'no-cache',
    connection: 'keep-alive',
  })

  if (info.kind === 'title') return streamText(res, 'DSH 接入回归')
  if (info.kind === 'compaction') return streamText(res, '压缩摘要：早期对话已折叠。')
  if (info.kind === 'main-followup') return streamText(res, '完成：计划已更新，echo=pong。')
  if (info.kind === 'main' && TEXT_ONLY) return streamText(res, '代理收尾验证：这是一段普通回答。')
  return streamMain(res, info)
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[mock-llm] listening on http://127.0.0.1:${PORT}`)
})
