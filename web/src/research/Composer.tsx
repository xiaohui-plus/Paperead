import { useEffect, useRef, useState } from 'react'
import { IconSend, IconStop } from '../components/icons'

interface Props {
  running: boolean
  placeholder?: string
  autoFocus?: boolean
  hint?: string
  onSend: (text: string) => void
  onStop: () => void
}

/** 会话输入框：Enter 发送、Shift+Enter 换行，输入法组合期间不发送。 */
export function Composer({
  running,
  placeholder = '描述你想研究的问题…',
  autoFocus = false,
  hint,
  onSend,
  onStop,
}: Props) {
  const [draft, setDraft] = useState('')
  const textarea = useRef<HTMLTextAreaElement>(null)
  // 手机端回车是换行键，不劫持成发送。
  const [touch] = useState(
    () =>
      typeof window !== 'undefined' &&
      Boolean(window.matchMedia?.('(pointer: coarse)').matches),
  )

  useEffect(() => {
    if (autoFocus) textarea.current?.focus()
  }, [autoFocus])

  const submit = () => {
    const text = draft.trim()
    if (!text) return
    setDraft('')
    onSend(text)
  }

  return (
    <div className="project-composer research-chat-composer">
      <textarea
        ref={textarea}
        value={draft}
        placeholder={placeholder}
        rows={2}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return
          if (event.key === 'Enter' && !event.shiftKey && !touch) {
            event.preventDefault()
            submit()
          }
        }}
      />
      <div className="project-composer-actions">
        <span>
          {hint ??
            (running
              ? '正在研究，可随时停止'
              : touch
                ? '点「发送」开始，回车换行'
                : 'Enter 发送 · Shift+Enter 换行')}
        </span>
        {running ? (
          <button className="research-stop" onClick={onStop}>
            <IconStop />
            停止
          </button>
        ) : (
          <button
            className="research-send"
            disabled={!draft.trim()}
            onClick={submit}
          >
            <IconSend />
            发送
          </button>
        )}
      </div>
    </div>
  )
}
