import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import type { Paper } from '../types'

interface Props {
  paper: Paper
  /** 左栏：原版 PDF（pdf.js） */
  pdfPane: ReactNode
  /** 右栏：译文 PDF（pdf.js 渲染同一套页级回填产物） */
  translationPane: ReactNode
  currentPage: number
  /** 是否显示左侧原文栏；关掉后译文占满 */
  showSource?: boolean
  onToggleSource?: () => void
  /** 两栏滚动是否联动（默认开） */
  syncEnabled?: boolean
  onToggleSync?: () => void
}

/**
 * 全文翻译双栏模式：左原版 PDF、右译文 PDF，两侧都是 pdf.js 页面。
 *
 * 字号由后端按每篇内容自动统一，这里不提供手动字号。
 * 左右默认逐页绑定：左栏翻页右栏跳同页，滚动按高度比例双向跟随。
 */
export function DualReader({
  paper,
  pdfPane,
  translationPane,
  currentPage,
  showSource = true,
  onToggleSource,
  syncEnabled = true,
  onToggleSync,
}: Props) {
  const [ratio, setRatio] = useState(0.5)
  const lockRef = useRef(false)

  useEffect(() => {
    if (!showSource || !syncEnabled) return
    const root = document.querySelector('.dual-reader')
    const left = root?.querySelector<HTMLElement>('.dual-pane-pdf .pdf-view')
    const right = root?.querySelector<HTMLElement>('.dual-pane-zh .pdf-view')
    if (!left || !right) return
    const sync = (from: HTMLElement, to: HTMLElement) => {
      if (lockRef.current) return
      const fromRange = from.scrollHeight - from.clientHeight
      const toRange = to.scrollHeight - to.clientHeight
      if (fromRange <= 0 || toRange <= 0) return
      lockRef.current = true
      to.scrollTop = (from.scrollTop / fromRange) * toRange
      requestAnimationFrame(() => {
        lockRef.current = false
      })
    }
    const onLeft = () => sync(left, right)
    const onRight = () => sync(right, left)
    left.addEventListener('scroll', onLeft, { passive: true })
    right.addEventListener('scroll', onRight, { passive: true })
    return () => {
      left.removeEventListener('scroll', onLeft)
      right.removeEventListener('scroll', onRight)
    }
  }, [showSource, syncEnabled])

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    const handle = event.currentTarget
    const box = handle.parentElement?.getBoundingClientRect()
    if (!box) return
    handle.setPointerCapture(event.pointerId)
    const move = (ev: PointerEvent) => {
      const next = (ev.clientX - box.left) / box.width
      setRatio(Math.min(0.72, Math.max(0.28, next)))
    }
    const up = () => {
      handle.removeEventListener('pointermove', move)
      handle.removeEventListener('pointerup', up)
    }
    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', up)
  }

  return (
    <div
      className={`dual-reader${showSource ? '' : ' is-single'}`}
      style={{ '--dual-split': `${ratio * 100}%` } as CSSProperties}
    >
      {showSource && <section className="dual-pane dual-pane-pdf">{pdfPane}</section>}
      {showSource && (
        <div
          className="dual-split"
          role="separator"
          aria-orientation="vertical"
          aria-label="拖动调整原文与译文宽度"
          title="拖动调整左右宽度"
          onPointerDown={startDrag}
        />
      )}
      <section className="dual-pane dual-pane-zh">
        <header className="dual-pane-head">
          <span>译文 PDF</span>
          <span className="dual-head-right">
            {onToggleSource && (
              <button
                type="button"
                className="dual-source-toggle"
                data-active={showSource}
                onClick={onToggleSource}
              >
                {showSource ? '关原文' : '开原文'}
              </button>
            )}
            {onToggleSync && (
              <button
                type="button"
                className="dual-source-toggle"
                data-active={syncEnabled}
                onClick={onToggleSync}
              >
                联动
              </button>
            )}
            <span>
              P{currentPage} / {paper.page_count}
            </span>
          </span>
        </header>
        <div className="dual-pane-body">{translationPane}</div>
      </section>
    </div>
  )
}