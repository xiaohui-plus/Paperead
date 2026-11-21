import {
  useEffect,

  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'

interface Props {
  /** 左栏：原版 PDF（pdf.js） */
  pdfPane: ReactNode
  /** 右栏：译文 PDF（pdf.js 渲染同一套页级回填产物） */
  translationPane: ReactNode
  /** 是否显示左侧原文栏；关掉后译文占满 */
  showSource?: boolean
  /** 两栏滚动是否联动（默认开） */
  syncEnabled?: boolean
  /** 右栏译文还在生成：显示顶部进度线条 */
  progress?: { done: number; total: number; building?: boolean } | null
  /** 右栏译文 PDF 是否已就绪；从骨架切到 PDF 后需要重新绑定滚动联动 */
  translationReady?: boolean
}

/**
 * 全文翻译双栏模式：左原版 PDF、右译文 PDF，两侧都是 pdf.js 页面。
 *
 * 字号由后端按每篇内容自动统一，这里不提供手动字号。
 * 左右不按页绑定：不随翻页跳转，只按滚动高度比例双向实时对齐。
 */
export function DualReader({
  pdfPane,
  translationPane,
  showSource = true,
  syncEnabled = true,
  progress = null,
  translationReady = true,
}: Props) {
  const [ratio, setRatio] = useState(0.5)

  useEffect(() => {
    if (!showSource || !syncEnabled) return
    const root = document.querySelector('.dual-reader')
    const left = root?.querySelector<HTMLElement>('.dual-pane-pdf .pdf-view')
    const right = root?.querySelector<HTMLElement>('.dual-pane-zh .pdf-view')
    if (!left || !right) return
    // 只忽略「程序写 scrollTop」引发的回环事件，不丢弃用户滚动，任何时刻都按最新位置跟随
    const expectedTop = new WeakMap<HTMLElement, number>()
    const sync = (from: HTMLElement, to: HTMLElement) => {
      const fromRange = from.scrollHeight - from.clientHeight
      const toRange = to.scrollHeight - to.clientHeight
      if (fromRange <= 0 || toRange <= 0) return false
      const target = (from.scrollTop / fromRange) * toRange
      if (Math.abs(to.scrollTop - target) < 0.5) return true
      expectedTop.set(to, target)
      to.scrollTop = target
      return true
    }
    const follow = (from: HTMLElement, to: HTMLElement) => {
      const expected = expectedTop.get(from)
      if (expected !== undefined) {
        expectedTop.delete(from)
        if (Math.abs(from.scrollTop - expected) < 1.5) return
      }
      sync(from, to)
    }
    // 绑定后先对齐一次：右栏 PDF 页面可能还没量出高度，短间隔重试直到能滚
    const timers: number[] = []
    let attempts = 0
    const align = () => {
      attempts += 1
      if (sync(left, right) || attempts >= 6) return
      timers.push(window.setTimeout(align, 250))
    }
    align()
    const onLeft = () => follow(left, right)
    const onRight = () => follow(right, left)
    left.addEventListener('scroll', onLeft, { passive: true })
    right.addEventListener('scroll', onRight, { passive: true })
    return () => {
      timers.forEach((id) => window.clearTimeout(id))
      left.removeEventListener('scroll', onLeft)
      right.removeEventListener('scroll', onRight)
    }
  }, [showSource, syncEnabled, translationReady])

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
      {showSource && (
        <section className="dual-pane dual-pane-pdf">
          <div className="dual-pane-body">{pdfPane}</div>
        </section>
      )}
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
        {progress && <DualProgress {...progress} />}
        {/* 控制项（关原文 / 联动 / 下载）统一放在应用顶栏最右侧，这里不再重复 */}
        <div className="dual-pane-body">{translationPane}</div>
      </section>
    </div>
  )
}

function DualProgress({
  done,
  total,
  building = false,
}: {
  done: number
  total: number
  building?: boolean
}) {
  const known = total > 0 && !building
  const percent = known ? Math.min(100, Math.round((done / total) * 100)) : 0
  return (
    <div
      className={`dual-progress${known ? '' : ' is-indeterminate'}`}
      role="progressbar"
      aria-label="译文生成进度"
      aria-valuemin={0}
      aria-valuemax={known ? total : undefined}
      aria-valuenow={known ? done : undefined}
    >
      <i style={known ? { width: `${percent}%` } : undefined} />
    </div>
  )
}