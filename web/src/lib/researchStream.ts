/** 会话事件流：fetch + ReadableStream 解析 SSE，断线指数退避重连。 */

import type { AgentEvent } from './researchTypes'

export type StreamState = 'connecting' | 'live' | 'offline' | 'error'

export interface StreamHandlers {
  onEvent: (event: AgentEvent) => void
  onState?: (state: StreamState, detail?: string) => void
  /** 断线重连过程中的提示，连接恢复后由 onState('live') 清除。 */
  onError?: (message: string) => void
}

const IDLE_TIMEOUT = 75_000
const MAX_BACKOFF = 15_000

export function streamSessionEvents(
  sessionId: string,
  after: number,
  handlers: StreamHandlers,
): () => void {
  let closed = false
  let cursor = Math.max(0, after)
  let attempt = 0
  let controller: AbortController | null = null
  let reconnectTimer: number | undefined
  let idleTimer: number | undefined
  let offline = false

  const clearIdle = () => {
    if (idleTimer !== undefined) window.clearTimeout(idleTimer)
    idleTimer = undefined
  }

  const armIdle = () => {
    clearIdle()
    idleTimer = window.setTimeout(() => controller?.abort(), IDLE_TIMEOUT)
  }

  const schedule = () => {
    if (closed || offline) return
    attempt += 1
    const delay = Math.min(MAX_BACKOFF, 500 * 2 ** Math.min(attempt, 5))
    handlers.onError?.('连接已断开，正在重连…')
    handlers.onState?.('error')
    reconnectTimer = window.setTimeout(() => void open(), delay)
  }

  const handleBlock = (block: string) => {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim())
      .join('')
    if (!data) return
    let event: AgentEvent
    try {
      event = JSON.parse(data) as AgentEvent
    } catch {
      return
    }
    if (!event || typeof event !== 'object') return
    if (typeof event.seq === 'number' && event.seq > cursor) cursor = event.seq
    if (event.code === 'agent.offline') offline = true
    handlers.onEvent(event)
  }

  const open = async () => {
    if (closed) return
    controller = new AbortController()
    armIdle()
    let buffer = ''
    try {
      const response = await fetch(
        `/api/research/sessions/${encodeURIComponent(sessionId)}/events?after=${cursor}`,
        { signal: controller.signal, headers: { Accept: 'text/event-stream' } },
      )
      if (!response.ok) {
        const data = (await response.json().catch(() => ({}))) as { detail?: string }
        throw new Error(data.detail || `连接失败（HTTP ${response.status}）`)
      }
      if (!response.body) throw new Error('当前浏览器不支持流式响应')

      attempt = 0
      handlers.onState?.('live')
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        armIdle()
        buffer += decoder.decode(value, { stream: true })
        const separator = /\r?\n\r?\n/
        let match = separator.exec(buffer)
        while (match) {
          handleBlock(buffer.slice(0, match.index))
          buffer = buffer.slice(match.index + match[0].length)
          match = separator.exec(buffer)
        }
      }
    } catch (error) {
      if (closed) return
      if ((error as { name?: string }).name === 'AbortError' && idleTimer !== undefined) {
        // 看门狗主动断开：直接进入重连。
      } else if ((error as { name?: string }).name === 'AbortError') {
        return
      } else {
        handlers.onError?.(
          error instanceof Error ? error.message : '连接中断',
        )
      }
    } finally {
      clearIdle()
    }
    if (closed) return
    if (offline) {
      handlers.onState?.('offline')
      return
    }
    schedule()
  }

  void open()

  return () => {
    closed = true
    controller?.abort()
    clearIdle()
    if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer)
  }
}
