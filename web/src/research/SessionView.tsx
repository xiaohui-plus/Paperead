import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ErrorBoundary } from '../components/ErrorBoundary'
import {
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
import {
  readSessionCache,
  writeSessionCache,
  type SessionSnapshot,
} from './researchCache'

interface Props {
  session: SessionSummary
  initialText: string | null
  onInitialTextConsumed: () => void
  /** 把标题/运行状态同步回左侧会话列表。 */
  onMeta: (patch: { title?: string; running?: boolean }) => void
  /** 当前会话标题，交给项目顶栏右侧单行渲染。 */
  onTitle?: (title: string) => void
  /** 输入框上传的附件，交给项目页刷新文件树。 */
  onFilesUploaded?: (files: ProjectFile[]) => void
  /** 打开设置并切到「配置模型」分组。 */
  onOpenSettings?: () => void
  /** 当前项目工作文件夹名，显示在消息框「/」右侧。 */
  workspaceName?: string
  onPickWorkspace?: () => void
  kind: ProjectKind
}

const FLUSH_MS = 40
/**
 * 历史回放期间要静默多久才算“历史到此为止”。
 *
 * 切页/切会话期间 agent 仍在跑，积压的历史可能有上万条事件；按固定超时截断
 * 会把还没读完的轮次误判成“已中断”。改成静默判定：只要事件还在源源不断
 * 到达就继续回放。
 */
const REPLAY_IDLE_MS = 1_200
/** 上限兜底：后端始终不发 replay.end 时也要把历史渲染出来。 */
const REPLAY_MAX_MS = 20_000

export function SessionView({
  session,
  initialText,
  onInitialTextConsumed,
  onMeta,
  onTitle,
  onFilesUploaded,
  onOpenSettings,
  workspaceName,
  onPickWorkspace,
  kind,
}: Props) {
  /** 上次离开时的快照：命中就先渲染，再从该 seq 增量补齐。 */
  const snapshot = useRef<SessionSnapshot | null>(
    readSessionCache(session.id),
  ).current
  const [chat, setChat] = useState<ChatState>(() => snapshot?.chat ?? EMPTY_CHAT)
  const [streamState, setStreamState] = useState<StreamState>('connecting')
  const [streamError, setStreamError] = useState<string | null>(null)
  const [sendError, setSendError] = useState<string | null>(null)
  const [streamKey, setStreamKey] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  const [historyLoading, setHistoryLoading] = useState(!snapshot)

  const cursor = useRef(snapshot?.cursor ?? 0)
  const pending = useRef<AgentEvent[]>([])
  const chatRef = useRef(chat)
  const flushTimer = useRef<number | undefined>(undefined)
  const initialSent = useRef(false)
  /** 历史回放阶段：先缓冲，收到 replay.end 后一次性渲染最终状态。 */
  const replaying = useRef(true)
  const replayEvents = useRef<AgentEvent[]>([])
  const replayTimer = useRef<number | undefined>(undefined)
  const replayStartedAt = useRef(0)

  const clearReplayTimer = useCallback(() => {
    if (replayTimer.current !== undefined) {
      window.clearTimeout(replayTimer.current)
      replayTimer.current = undefined
    }
  }, [])

  const finishReplay = useCallback(
    (running: boolean | undefined, lastSeq?: number) => {
      if (!replaying.current) {
        // 回放结束后才到的 replay.end（例如断线重连）：正文与运行状态都按实时事件
        // 收敛，这里只同步左侧会话指示，避免用可能滞后的状态覆盖正在进行的轮次。
        if (running === true) onMeta({ running: true })
        return
      }
      replaying.current = false
      replayStartedAt.current = 0
      clearReplayTimer()
      const events = replayEvents.current
      replayEvents.current = []
      let title: string | null = null
      let seq = cursor.current
      if (typeof lastSeq === 'number' && lastSeq > seq) seq = lastSeq
      for (const event of events) {
        if (event.type === 'title.update' && event.title) title = event.title
        if (event.seq > seq) seq = event.seq
      }
      cursor.current = seq
      // 兜底超时拿不到 agent 侧状态时，以事件本身为准：最后一个轮次没有 turn.end
      // 就保持运行态，不能把还在跑的轮次误判成“已中断”。
      const base = applyHistoryEvents(chatRef.current, events)
      const last = base.turns[base.turns.length - 1]
      const active =
        typeof running === 'boolean'
          ? running
          : Boolean(last && last.endedAt === null)
      setChat((prev) => finalizeHistory(applyHistoryEvents(prev, events), active))
      setHistoryLoading(false)
      if (title) onMeta({ title })
      onMeta({ running: active })
    },
    [clearReplayTimer, onMeta],
  )

  /** 回放兜底：事件持续到达就一直顺延，静默或超上限后才结束。 */
  const armReplayTimer = useCallback(() => {
    if (replayStartedAt.current === 0) replayStartedAt.current = Date.now()
    if (replayTimer.current !== undefined) window.clearTimeout(replayTimer.current)
    const elapsed = Date.now() - replayStartedAt.current
    const wait = Math.max(0, Math.min(REPLAY_IDLE_MS, REPLAY_MAX_MS - elapsed))
    replayTimer.current = window.setTimeout(() => {
      replayTimer.current = undefined
      finishReplay(undefined)
    }, wait)
  }, [finishReplay])

  const flush = useCallback(() => {
    flushTimer.current = undefined
    const events = pending.current
    pending.current = []
    if (events.length === 0) return
    let seq = cursor.current
    for (const event of events) {
      if (event.seq > seq) seq = event.seq
    }
    cursor.current = seq
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
        finishReplay(event.running === true, event.lastSeq)
        return
      }
      if (replaying.current) {
        replayEvents.current.push(event)
        armReplayTimer()
        return
      }
      pending.current.push(event)
      if (flushTimer.current === undefined) {
        flushTimer.current = window.setTimeout(flush, FLUSH_MS)
      }
    },
    [armReplayTimer, finishReplay, flush],
  )

  useEffect(() => {
    setStreamState('connecting')
    if (!snapshot && cursor.current === 0) {
      // 没有快照才需要等历史回放结束；有快照时先显示快照再补增量。
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
  }, [session.id, streamKey, handleEvent, snapshot])

  useEffect(() => {
    chatRef.current = chat
    if (replaying.current) return
    writeSessionCache(session.id, chat, cursor.current)
  }, [chat, session.id])

  useEffect(
    () => () => {
      if (flushTimer.current !== undefined) window.clearTimeout(flushTimer.current)
      if (replayTimer.current !== undefined) {
        window.clearTimeout(replayTimer.current)
      }
      // 历史还没回放完就离开时不要覆盖旧快照，否则下次会缺一段历史。
      // 但命中快照时本身已有完整的历史基线，可以安全写回当前状态。
      if (replaying.current && !snapshot) return
      const queued = pending.current
      pending.current = []
      if (queued.length > 0) {
        let seq = cursor.current
        for (const event of queued) {
          if (event.seq > seq) seq = event.seq
        }
        const next = applyEvents(chatRef.current, queued)
        cursor.current = seq
        chatRef.current = next
        writeSessionCache(session.id, next, seq)
        return
      }
      writeSessionCache(session.id, chatRef.current, cursor.current)
    },
    [session.id],
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

  useEffect(() => {
    onTitle?.(pageTitle)
  }, [pageTitle, onTitle])

  return (
    <div className="project-session-view">
      {streamError && streamState === 'error' && (
        <p className="session-banner">
          <span>{streamError}</span>
          <button
            className="session-retry"
            onClick={() => setStreamKey((key) => key + 1)}
          >
            重连
          </button>
        </p>
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
          onOpenSettings={onOpenSettings}
          workspaceName={workspaceName}
          onPickWorkspace={onPickWorkspace}
          onCommandResult={(event) =>
            setChat((prev) => applyEvent(prev, event))
          }
        />
      )}
    </div>
  )
}
