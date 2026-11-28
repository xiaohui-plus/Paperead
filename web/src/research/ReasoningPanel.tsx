import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { IconBrain, IconChevron } from '../components/icons'

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
}

/**
 * 思考行：对齐 DSH 的 ReasoningRow。
 * 单行独立折叠，默认收起；运行中摘要跟随最后一行，结束后退回第一行。
 * 一段思考结束（或进入下一步任务）时自动收起，用户此前展开过也一并折叠，
 * 避免旧思考长期占位。
 */
export function ReasoningPanel({ text, running }: Props) {
  const [open, setOpen] = useState(false)
  const summaryRef = useRef<HTMLSpanElement>(null)
  const wasRunning = useRef(running)
  const summary = running ? latestLine(text) : firstLine(text)

  useLayoutEffect(() => {
    const element = summaryRef.current
    if (!element) return
    element.scrollLeft = running ? element.scrollWidth - element.clientWidth : 0
  }, [running, summary])

  useEffect(() => {
    if (wasRunning.current && !running) setOpen(false)
    wasRunning.current = running
  }, [running])

  return (
    <div className="reasoning-panel" data-state={running ? 'running' : 'ok'} data-open={open}>
      <button
        className="reasoning-row"
        aria-expanded={open}
        aria-label={running ? '正在思考' : '思考过程'}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="reasoning-leading">
          <IconBrain />
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
        <span className="reasoning-chevron" data-open={open}>
          <IconChevron />
        </span>
      </button>
      {open && <pre className="reasoning-body">{text}</pre>}
    </div>
  )
}
