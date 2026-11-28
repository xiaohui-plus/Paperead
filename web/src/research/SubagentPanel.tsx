import { useEffect, useState } from 'react'
import { IconBot } from '../components/icons'
import { researchApi, type SessionSubagent } from '../lib/researchApi'
import { applyEvent } from '../lib/researchReducer'
import { streamSubagentEvents, type StreamState } from '../lib/researchStream'
import { EMPTY_CHAT, type ChatState } from '../lib/researchTypes'
import { Transcript } from './Transcript'

interface Props {
  parentSessionId: string
  open: boolean
  onClose: () => void
}

const POLL_MS = 3_000

/**
 * 子代理面板：列出父会话派生的子代理，点击后订阅子代理子会话的实时事件。
 *
 * 子代理是 agent 内部的独立 session，不落产品库，也不出现在左侧会话列表；
 * 这里按需轮询列表 + 打开时直连 live SSE，关闭面板即断开。
 */
export function SubagentPanel({ parentSessionId, open, onClose }: Props) {
  const [items, setItems] = useState<SessionSubagent[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [chat, setChat] = useState<ChatState>(EMPTY_CHAT)
  const [state, setState] = useState<StreamState>('connecting')
  const [error, setError] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!open) return
    let cancelled = false
    const load = async () => {
      try {
        const rows = await researchApi.listSubagents(parentSessionId)
        if (!cancelled) {
          setItems(rows)
          setError(null)
        }
      } catch {
        if (!cancelled) setError('无法读取子代理列表')
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [open, parentSessionId])

  useEffect(() => {
    if (!open) return
    if (items.length === 0) {
      if (activeId) setActiveId(null)
      return
    }
    if (!activeId || !items.some((item) => item.id === activeId)) {
      setActiveId(items[0].id)
    }
  }, [open, items, activeId])

  useEffect(() => {
    if (!open || !activeId) return
    setChat(EMPTY_CHAT)
    setState('connecting')
    const close = streamSubagentEvents(activeId, 0, {
      onEvent: (event) => {
        if (event.type === 'replay.end') return
        setChat((prev) => applyEvent(prev, event))
      },
      onState: setState,
      onError: (message) => setError(message),
    })
    return close
  }, [open, activeId])

  useEffect(() => {
    if (!chat.running) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [chat.running])

  if (!open) return null

  const active = items.find((item) => item.id === activeId) ?? null
  const loading = chat.turns.length === 0 && state !== 'offline'

  return (
    <aside className="subagent-panel" aria-label="子代理">
      <header className="subagent-head">
        <IconBot />
        <strong>子代理</strong>
        <span className="subagent-count">{items.length}</span>
        <button type="button" className="subagent-close" onClick={onClose} aria-label="关闭子代理">
          ×
        </button>
      </header>
      <div className="subagent-body">
        <nav className="subagent-list">
          {items.length === 0 && (
            <p className="subagent-empty">当前会话还没有派生出的子代理。</p>
          )}
          {items.map((item) => {
            const running = item.status === 'running' || item.activity === 'active'
            return (
              <button
                type="button"
                key={item.id}
                className="subagent-item"
                data-active={item.id === activeId || undefined}
                onClick={() => setActiveId(item.id)}
              >
                <span className="subagent-dot" data-running={running || undefined} />
                <span className="subagent-label">
                  {item.label || (item.mode === 'continuable' ? '可继续子代理' : '一次性子代理')}
                </span>
                <span className="subagent-meta">{running ? '运行中' : '已结束'}</span>
              </button>
            )
          })}
        </nav>
        <section className="subagent-transcript">
          {active ? (
            <>
              <div className="subagent-transcript-head">
                <span>{active.label || active.id.slice(0, 8)}</span>
                <em>
                  {state === 'live' ? '实时' : state === 'offline' ? '已结束' : '连接中…'}
                </em>
              </div>
              <Transcript chat={chat} now={now} loading={loading} />
            </>
          ) : (
            <p className="subagent-empty">选择一个子代理查看输出。</p>
          )}
          {error && <p className="subagent-error">{error}</p>}
        </section>
      </div>
    </aside>
  )
}