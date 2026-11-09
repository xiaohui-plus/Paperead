import { useLayoutEffect, useRef, useState } from 'react'
import { IconChevron, IconSpark } from '../components/icons'

function firstLine(text: string): string {
  const newline = text.indexOf('\n')
  return newline === -1 ? text : text.slice(0, newline)
}

function latestLine(text: string): string {
  const visible = text.trimEnd()
  const newline = visible.lastIndexOf('\n')
  return newline === -1 ? visible : visible.slice(newline + 1)
}

interface Props {
  text: string
  running: boolean
  duration?: string
}

/**
 * 思考行：对齐 DeepSeek Harness 的 ReasoningRow。
 * 运行中单行摘要跟随最后一行并横向滚到行尾，结束后退回第一行。
 */
export function ReasoningPanel({ text, running, duration }: Props) {
  const [open, setOpen] = useState(false)
  const summaryRef = useRef<HTMLSpanElement>(null)
  const summary = running ? latestLine(text) : firstLine(text)

  useLayoutEffect(() => {
    const element = summaryRef.current
    if (!element) return
    element.scrollLeft = running ? element.scrollWidth - element.clientWidth : 0
  }, [running, summary])

  return (
    <div className="reasoning-panel" data-state={running ? 'running' : 'ok'}>
      <button
        className="reasoning-row"
        aria-expanded={open}
        aria-label={running ? '正在思考' : '思考过程'}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="reasoning-leading">
          <IconSpark />
        </span>
        <span className="reasoning-title">思考</span>
        {summary && (
          <>
            <span className="reasoning-sep" aria-hidden="true" />
            <span
              className="reasoning-summary"
              data-follow-end={running ? 'true' : undefined}
              ref={summaryRef}
            >
              {summary}
            </span>
          </>
        )}
        {!running && duration && <em className="reasoning-suffix">{duration}</em>}
        <span className="reasoning-chevron" data-open={open}>
          <IconChevron />
        </span>
      </button>
      {open && <pre className="reasoning-body">{text}</pre>}
    </div>
  )
}
