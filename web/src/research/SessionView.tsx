import { useCallback, useEffect, useRef, useState } from 'react'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { relativeTime, researchApi, type ProjectFile, type SessionSummary } from '../lib/researchApi'
import { EMPTY_CHAT, type AgentEvent, type ChatState } from '../lib/researchTypes'
import { addLocalMessage, applyEvents } from '../lib/researchReducer'
import { streamSessionEvents, type StreamState } from '../lib/researchStream'
import { Composer } from './Composer'
import { Transcript } from './Transcript'

interface Props {
  session: SessionSummary
  initialText: string | null
  onInitialTextConsumed: () => void
  /** 把标题/运行状态同步回左侧会话列表。 */
  onMeta: (patch: { title?: string; running?: boolean }) => void
  /** 输入框上传的附件，交给项目页刷新文件树。 */
  onFilesUploaded?: (files: ProjectFile[]) => void
}

const FLUSH_MS = 40

export function SessionView({
  session,
  initialText,
  onInitialTextConsumed,
  onMeta,
  onFilesUploaded,
}: Props) {
  const [chat, setChat] = useState<ChatState>(EMPTY_CHAT)
  const [streamState, setStreamState] = useState<StreamState>('connecting')
  const [streamError, setStreamError] = useState<string | null>(null)
  const [sendError, setSendError] = useState<string | null>(null)
  const [streamKey, setStreamKey] = useState(0)
  const [now, setNow] = useState(() => Date.now())

  const cursor = useRef(0)
  const pending = useRef<AgentEvent[]>([])
  const flushTimer = useRef<number | undefined>(undefined)
  const initialSent = useRef(false)

  const flush = useCallback(() => {
    flushTimer.current = undefined
    const events = pending.current
    pending.current = []
    if (events.length === 0) return
    setChat((prev) => applyEvents(prev, events))
    for (const event of events) {
      if (event.type === 'title.update' && event.title) {
        onMeta({ title: event.title })
      } else if (event.type === 'turn.start') {
        onMeta({ running: true })
      } else if (event.type === 'turn.end') {
        onMeta({ running: false })
      }
    }
  }, [onMeta])

  const handleEvent = useCallback(
    (event: AgentEvent) => {
      if (typeof event.seq === 'number' && event.seq > cursor.current) {
        cursor.current = event.seq
      }
      pending.current.push(event)
      if (flushTimer.current === undefined) {
        flushTimer.current = window.setTimeout(flush, FLUSH_MS)
      }
    },
    [flush],
  )

  useEffect(() => {
    setStreamState('connecting')
    const close = streamSessionEvents(session.id, cursor.current, {
      onEvent: handleEvent,
      onState: (state) => {
        setStreamState(state)
        if (state === 'live') setStreamError(null)
      },
      onError: (message) => setStreamError(message),
    })
    return close
  }, [session.id, streamKey, handleEvent])

  useEffect(
    () => () => {
      if (flushTimer.current !== undefined) window.clearTimeout(flushTimer.current)
    },
    [],
  )

  useEffect(() => {
    if (!chat.running) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [chat.running])

  const submit = useCallback(
    async (text: string) => {
      const value = text.trim()
      if (!value) return
      setSendError(null)
      setChat((prev) => addLocalMessage(prev, value))
      try {
        await researchApi.sendMessage(session.id, value)
        if (streamState === 'offline' || streamState === 'error') {
          setStreamKey((key) => key + 1)
        }
      } catch (error) {
        setSendError(
          error instanceof Error && error.message === 'AGENT_UNAVAILABLE'
            ? '研究 agent 未启动或已断开，消息已记录，启动后重试即可。'
            : error instanceof Error
              ? error.message
              : '发送失败，请重试',
        )
      }
    },
    [session.id, streamState],
  )

  useEffect(() => {
    if (!initialText || initialSent.current) return
    initialSent.current = true
    void submit(initialText)
    onInitialTextConsumed()
  }, [initialText, submit, onInitialTextConsumed])

  const stop = async () => {
    setSendError(null)
    try {
      await researchApi.cancelSession(session.id)
    } catch {
      setSendError('停止失败：研究 agent 未连接')
    }
  }

  const reconnecting = streamState === 'connecting' || streamState === 'error'

  return (
    <div className="project-session-view">
      <header className="project-session-head">
        <h2>{chat.title || session.title}</h2>
        <div className="session-head-meta">
          <span>{relativeTime(session.updatedAt)}</span>
          {chat.running && <i className="project-running" />}
          {(streamState === 'offline' || streamState === 'error') && (
            <button
              className="session-retry"
              onClick={() => setStreamKey((key) => key + 1)}
            >
              重连
            </button>
          )}
        </div>
      </header>

      {streamError && streamState === 'error' && (
        <p className="session-banner">{streamError}</p>
      )}
      {streamState === 'offline' && (
        <p className="session-banner" data-level="warn">
          研究 agent 尚未启动：历史消息可查看，发送会先记录再等待 agent。
        </p>
      )}
      {sendError && (
        <p className="session-banner" data-level="error">
          {sendError}
        </p>
      )}

      <ErrorBoundary
        key={session.id}
        title="这段会话渲染失败"
        hint="历史消息仍保存在本地。可以重试，或切换到其他会话后再回来。"
      >
        <Transcript chat={chat} now={now} />
      </ErrorBoundary>

      <Composer
        running={chat.running}
        projectId={session.projectId}
        onUploaded={(file) => onFilesUploaded?.([file])}
        hint={
          reconnecting && chat.messages.length === 0
            ? '正在连接研究 agent…'
            : undefined
        }
        onSend={(text) => void submit(text)}
        onStop={() => void stop()}
      />
    </div>
  )
}
