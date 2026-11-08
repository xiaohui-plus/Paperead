import { useEffect, useRef, useState } from 'react'

type SpikeEvent = { seq?: number; type: string; [key: string]: unknown }

/**
 * A0 spike page: proves the Python proxy -> Node agent -> SSE path from inside
 * the desktop shell. It is intentionally temporary; A3 replaces it with the
 * real research pages.
 */
export function SpikePage() {
  const [sessionId] = useState(() => `spike-${crypto.randomUUID()}`)
  const [events, setEvents] = useState<SpikeEvent[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const readerRef = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null)

  useEffect(() => {
    let cancelled = false

    async function connect() {
      try {
        const created = await fetch('/api/research/sessions', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ sessionId, cwd: '' }),
        })
        if (!created.ok) throw new Error(await created.text())

        const response = await fetch(
          `/api/research/sessions/${encodeURIComponent(sessionId)}/events?after=0`,
        )
        if (!response.ok) throw new Error(await response.text())
        if (!response.body) throw new Error('SSE body missing')

        const reader = response.body.getReader()
        readerRef.current = reader
        const decoder = new TextDecoder()
        let buffer = ''
        while (!cancelled) {
          const { value, done } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const frames = buffer.split('\n\n')
          buffer = frames.pop() || ''
          for (const frame of frames) {
            const data = frame.match(/^data: (.+)$/m)?.[1]
            if (!data) continue
            try {
              const event = JSON.parse(data) as SpikeEvent
              setEvents((prev) => [...prev.slice(-199), event])
            } catch {
              // ignore malformed frame
            }
          }
        }
      } catch (e) {
        if (!cancelled) setError(String((e as Error).message || e))
      }
    }

    connect()
    return () => {
      cancelled = true
      readerRef.current?.cancel().catch(() => {})
    }
  }, [sessionId])

  async function send() {
    setBusy(true)
    setError('')
    try {
      const res = await fetch(`/api/research/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text: '请回声验证工具链路' }),
      })
      if (!res.ok) throw new Error(await res.text())
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main style={{ maxWidth: 900, margin: '48px auto', padding: '0 24px', fontFamily: 'system-ui' }}>
      <h1>研究 agent · A0 spike</h1>
      <p style={{ color: '#666' }}>
        会话 <code>{sessionId.slice(0, 18)}…</code> · 经 Python 代理
      </p>
      <button onClick={send} disabled={busy} style={{ padding: '8px 14px' }}>
        {busy ? '发送中…' : '发送测试消息'}
      </button>
      {error && <pre style={{ color: '#b00020', whiteSpace: 'pre-wrap' }}>{error}</pre>}
      <div style={{ marginTop: 20, borderTop: '1px solid #eee' }}>
        {events.map((event, index) => (
          <div
            key={`${event.seq ?? index}-${index}`}
            style={{
              padding: '8px 10px',
              margin: '8px 0',
              background: '#f7f7f7',
              borderRadius: 8,
              fontSize: 13,
            }}
          >
            <strong>{event.type}</strong>
            {event.type === 'text.delta' && <span> · {String(event.text)}</span>}
            {event.type === 'reasoning.delta' && <span style={{ color: '#777' }}> · 思考</span>}
            {event.type === 'tool.start' && <span> · {String(event.name)}</span>}
            {event.type === 'tool.end' && <span> · {event.isError ? '失败' : '完成'}</span>}
            {event.type === 'title.update' && <span> · {String(event.title)}</span>}
            {event.type === 'turn.end' && <span> · {JSON.stringify(event.reason)}</span>}
          </div>
        ))}
      </div>
    </main>
  )
}
