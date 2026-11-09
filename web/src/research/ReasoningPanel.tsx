import { useState } from 'react'
import { IconChevron, IconSpark } from '../components/icons'

/** 思维链：默认折叠，关闭时不渲染正文（DOM 里没有 reasoning 文本）。 */
export function ReasoningPanel({
  text,
  running,
}: {
  text: string
  running: boolean
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="reasoning-panel">
      <button
        className="reasoning-toggle"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <IconSpark />
        <span>{running ? '正在思考…' : '思考过程'}</span>
        <span className="reasoning-chevron" data-open={open}>
          <IconChevron />
        </span>
      </button>
      {open && <pre className="reasoning-body">{text}</pre>}
    </div>
  )
}
