import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ErrorBoundary } from '../components/ErrorBoundary'
import {
  relativeTime,
  researchApi,
  type ProjectFile,
  type ProjectKind,
  type SendMeta,
  type SessionSummary,
} from '../lib/researchApi'
import { EMPTY_CHAT, type AgentEvent, type ChatState } from '../lib/researchTypes'
import {
  addLocalMessage,
  applyEvent,
  applyEvents,
  applyHistoryEvents,
  finalizeHistory,
} from '../lib/researchReducer'
import { ensureResearchModel } from '../lib/researchModel'
import { streamSessionEvents, type StreamState } from '../lib/researchStream'
import { ApprovalPanel } from './ApprovalPanel'
import { Composer } from './Composer'
import { splitLeadHeading } from './leadHeading'
import { Transcript } from './Transcript'

interface Props {
  session: SessionSummary
  initialText: string | null
  onInitialTextConsumed: () => void
  /** 把标题/运行状态同步回左侧会话列表。 */
  onMeta: (patch: { title?: string; running?: boolean }) => void
  /** 输入框上传的附件，交给项目页刷新文件树。 */
  onFilesUploaded?: (files: ProjectFile[]) => void
  kind: ProjectKind
}

const FLUSH_MS = 40
/** 旧版后端没有 replay.end 时的兜底：按历史快照渲染当前已收到的部分。 */
const REPLAY_FALLBACK_MS = 15_000

export function SessionView({
  session,
  initialText,
  onInitialTextConsumed,
  onMeta,
  onFilesUploaded,
  kind,
}: Props) {
  const [chat, setChat] = useState<ChatState>(EMPTY_CHAT)
  const [streamState, setStreamState] = useState<StreamState>('connecting')
  const [streamError, setStreamError] = useState<string | null>(null)
  const [sendError, setSendError] = useState<string | null>(null)
  const [streamKey, setStreamKey] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  const [historyLoading, setHistoryLoading] = useState(true)

  const cursor = useRef(0)
  const pending = useRef<AgentEvent[]>([])
  const flushTimer = useRef<number | undefined>(undefined)
  const initialSent = useRef(false)
  /** 历史回放阶段：先缓冲，收到 replay.end 后一次性渲染最终状态。 */
  const replaying = useRef(true)
  const replayEvents = useRef<AgentEvent[]>([])
  const replayTimer = useRef<number | undefined>(undefined)

  const clearReplayTimer = useCallback(() => {
    if (replayTimer.current !== undefined) {
      window.clearTimeout(replayTimer.current)
      replayTimer.current = undefined
    }
  }, [])

  const finishReplay = useCallback(
    (running: boolean) => {
      if (!replaying.current) return
      replaying.current = false
      clearReplayTimer()
      const events = replayEvents.current
      replayEvents.current = []
      let title: string | null = null
      for (const event of events) {
        if (event.type === 'title.update' && event.title) title = event.title
      }
      setChat((prev) => finalizeHistory(applyHistoryEvents(prev, events), running))
      setHistoryLoading(false)
      if (title) onMeta({ title })
      onMeta({ running })
    },
    [clearReplayTimer, onMeta],
  )

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
      if (event.type === 'replay.end') {
        finishReplay(event.running === true)
        return
      }
      if (typeof event.seq === 'number' && event.seq > cursor.current) {
        cursor.current = event.seq
      }
      if (replaying.current) {
        replayEvents.current.push(event)
        if (replayTimer.current === undefined) {
          replayTimer.current = window.setTimeout(
            () => finishReplay(false),
            REPLAY_FALLBACK_MS,
          )
        }
        return
      }
      pending.current.push(event)
      if (flushTimer.current === undefined) {
        flushTimer.current = window.setTimeout(flush, FLUSH_MS)
      }
    },
    [finishReplay, flush],
  )

  useEffect(() => {
    setStreamState('connecting')
    if (cursor.current === 0) {
      // 首次进入会话：等 replay.end，把历史一次性画成最终状态。
      replaying.current = true
      setHistoryLoading(true)
    }
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
      if (replayTimer.current !== undefined) {
        window.clearTimeout(replayTimer.current)
      }
    },
    [],
  )

  useEffect(() => {
    if (!chat.running) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [chat.running])

  const submit = useCallback(
    async (text: string, meta?: SendMeta) => {
      const value = text.trim()
      if (!value) return
      setSendError(null)
      const modelProblem = await ensureResearchModel()
      if (modelProblem) {
        setSendError(modelProblem)
        return
      }
      setChat((prev) => addLocalMessage(prev, value, meta))
      try {
        await researchApi.sendMessage(session.id, value, meta)
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

  const approval = chat.approvals[0]
  const approvalCommand = useMemo(() => {
    if (!approval?.callId) return undefined
    for (const turn of chat.turns) {
      const tool = turn.tools.find((item) => item.callId === approval.callId)
      const input = tool?.input
      if (input && typeof input === 'object') {
        const command = (input as Record<string, unknown>).command
        if (typeof command === 'string' && command.trim()) return command
      }
    }
    return undefined
  }, [approval, chat.turns])

  const answerApproval = async (outcome: 'allowed-once' | 'rejected') => {
    if (!approval) return
    await researchApi.respondApproval(session.id, approval.requestId, outcome)
  }

  const pageTitle = useMemo(() => {
    for (let index = chat.turns.length - 1; index >= 0; index -= 1) {
      const turn = chat.turns[index]
      const lead = splitLeadHeading(turn.text)
      if (lead && (lead.complete || turn.endedAt !== null)) return lead.title
    }
    return chat.title || session.title
  }, [chat.turns, chat.title, session.title])

  return (
    <div className="project-session-view">
      <header className="project-session-head">
        <h2>{pageTitle}</h2>
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
        <Transcript chat={chat} now={now} loading={historyLoading} />
      </ErrorBoundary>

      {approval ? (
        <ApprovalPanel
          approval={approval}
          command={approvalCommand}
          onAnswer={answerApproval}
        />
      ) : (
        <Composer
          running={chat.running}
          kind={kind}
          projectId={session.projectId}
          sessionId={session.id}
          permission={chat.permission}
          empty={!historyLoading && chat.turns.length === 0}
          onUploaded={(file) => onFilesUploaded?.([file])}
          hint={
            reconnecting && chat.messages.length === 0
              ? '正在连接研究 agent…'
              : undefined
          }
          onSend={(text, meta) => void submit(text, meta)}
          onStop={() => void stop()}
          onCommandResult={(event) =>
            setChat((prev) => applyEvent(prev, event))
          }
        />
      )}
    </div>
  )
}
