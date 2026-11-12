import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { IconChat } from '../components/icons'
import type { ChatState } from '../lib/researchTypes'
import { TurnBlock } from './TurnBlock'

/** 超过约 10 行就折起，避免用户粘贴长提示时把会话流撑得很长。 */
const COLLAPSED_HEIGHT = 180

function MessageBubble({ text }: { text: string }) {
  const bubble = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [collapsible, setCollapsible] = useState(false)

  useLayoutEffect(() => {
    const element = bubble.current
    if (!element) return
    // scrollHeight 始终是完整内容高度，收起状态下也能测出是否超过阈值。
    setCollapsible(element.scrollHeight > COLLAPSED_HEIGHT + 1)
  }, [text, expanded])

  return (
    <>
      <div
        ref={bubble}
        className={`chat-bubble${
          collapsible && !expanded ? ' is-collapsed' : ''
        }`}
      >
        {text}
      </div>
      {collapsible && (
        <button
          type="button"
          className="chat-bubble-toggle"
          onClick={() => setExpanded((prev) => !prev)}
        >
          {expanded ? '收起' : '展示更多'}
        </button>
      )}
    </>
  )
}

interface Props {
  chat: ChatState
  now: number
  /** 历史快照还在加载：不要把空会话文案误显示成“开始这段会话”。 */
  loading?: boolean
}

type Row =
  | {
      kind: 'message'
      id: string
      at: number
      text: string
      mode?: string
      scope?: string
    }
  | { kind: 'turn'; id: string; at: number; turn: ChatState['turns'][number] }

function buildRows(chat: ChatState): Row[] {
  const rows: Row[] = chat.messages.map((message) => ({
    kind: 'message',
    id: message.id,
    at: message.at,
    text: message.text,
    mode: message.mode,
    scope: message.scope,
  }))
  for (const turn of chat.turns) {
    rows.push({
      kind: 'turn',
      id: `turn-${turn.turn}`,
      at: turn.startedAt,
      turn,
    })
  }
  return rows.sort((a, b) => a.at - b.at)
}

/** 转写：用户消息 → 助手轮次；只有停在底部时才自动跟随。 */
export function Transcript({ chat, now, loading = false }: Props) {
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const rows = useMemo(() => buildRows(chat), [chat])

  useEffect(() => {
    const element = scroller.current
    if (!element) return
    if (stick.current) {
      element.scrollTop = element.scrollHeight
    }
  }, [rows])

  const onScroll = () => {
    const element = scroller.current
    if (!element) return
    stick.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < 140
  }

  if (loading && rows.length === 0 && chat.notices.length === 0) {
    return (
      <div className="project-transcript" data-loading="true">
        <IconChat />
        <p>正在加载会话历史…</p>
      </div>
    )
  }

  if (rows.length === 0 && chat.notices.length === 0) {
    return (
      <div className="project-transcript">
        <IconChat />
        <p>开始这段会话</p>
        <span>描述研究问题，或先在左侧上传一份文献。</span>
      </div>
    )
  }

  return (
    <div className="project-transcript-scroll" ref={scroller} onScroll={onScroll}>
      <div className="project-transcript-body">
        {rows.map((row) =>
          row.kind === 'message' ? (
            <div className="chat-message" key={row.id}>
              <MessageBubble text={row.text} />
            </div>
          ) : (
            <TurnBlock key={row.id} turn={row.turn} now={now} />
          ),
        )}
        {chat.notices.map((notice) => (
          <p className="chat-notice" data-level={notice.level} key={notice.id}>
            {notice.text}
          </p>
        ))}
      </div>
    </div>
  )
}
