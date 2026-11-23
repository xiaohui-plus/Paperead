import {
  useEffect,
  useLayoutEffect,
  useRef,
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
  /** 两栏共同使用的页面放大倍率（1 = 适配宽度）；缩放后两栏锚点保持一致 */
  zoom?: number
}

type PaneScroll = { top: number; left: number }

/**
 * 把 from 栏的滚动态映射到 to 栏：纵向与横向都按可滚范围比例对齐。
 *
 * 两栏页数、页面尺寸一致，同一滚动比例天然对应「同一页 + 同一页内位置」，
 * 放大后仍然成立；横向同理（例如一起停在某一页的左上角）。
 * 返回 false 表示两栏都还不可滚，需要稍后重试。
 */
function syncPane(
  from: HTMLElement,
  to: HTMLElement,
  expected: WeakMap<HTMLElement, PaneScroll>,
): boolean {
  const fromY = from.scrollHeight - from.clientHeight
  const toY = to.scrollHeight - to.clientHeight
  const fromX = from.scrollWidth - from.clientWidth
  const toX = to.scrollWidth - to.clientWidth
  let nextTop = to.scrollTop
  let nextLeft = to.scrollLeft
  let synced = false
  if (fromY > 0 && toY > 0) {
    nextTop = Math.min(toY, Math.max(0, (from.scrollTop / fromY) * toY))
    synced = true
  }
  if (fromX > 0 && toX > 0) {
    nextLeft = Math.min(toX, Math.max(0, (from.scrollLeft / fromX) * toX))
    synced = true
  }
  if (!synced) return false
  if (Math.abs(nextTop - to.scrollTop) < 0.5 && Math.abs(nextLeft - to.scrollLeft) < 0.5) {
    return true
  }
  expected.set(to, { top: nextTop, left: nextLeft })
  to.scrollTop = nextTop
  to.scrollLeft = nextLeft
  return true
}

/**
 * 全文翻译双栏模式：左原版 PDF、右译文 PDF，两侧都是 pdf.js 页面。
 *
 * 字号由后端按每篇内容自动统一，这里不提供手动字号。
 * 左右不按页绑定：滚动时纵向、横向都双向实时对齐；
 * 放大/缩小时两栏保持同一视野（例如一起停在某一页的左上角）。
 */
export function DualReader({
  pdfPane,
  translationPane,
  showSource = true,
  syncEnabled = true,
  progress = null,
  translationReady = true,
  zoom = 1,
}: Props) {
  const [ratio, setRatio] = useState(0.5)
  const expectedPos = useRef(new WeakMap<HTMLElement, PaneScroll>())
  const prevZoom = useRef(zoom)

  useEffect(() => {
    if (!showSource || !syncEnabled) return
    const root = document.querySelector<HTMLElement>('.dual-reader')
    if (!root) return
    const expected = expectedPos.current
    let boundLeft: HTMLElement | null = null
    let boundRight: HTMLElement | null = null
    let frameLeft = 0
    let frameRight = 0
    let alignFrame = 0
    let watcher: MutationObserver | null = null
    let resizeObserver: ResizeObserver | null = null
    const timers: number[] = []
    // 最近一次真正由用户滚动触发的栏：尺寸变化后要按它对齐，不能总是把右栏拉回左栏
    let driver: HTMLElement | null = null
    let lastUserScroll = 0

    // 只忽略「程序写 scrollTop / scrollLeft」引发的回环事件，不丢弃用户滚动，任何时刻都按最新位置跟随
    const follow = (from: HTMLElement, to: HTMLElement) => {
      const exp = expected.get(from)
      if (exp) {
        expected.delete(from)
        if (
          Math.abs(from.scrollTop - exp.top) < 1.5 &&
          Math.abs(from.scrollLeft - exp.left) < 1.5
        ) {
          return
        }
      }
      driver = from
      lastUserScroll = performance.now()
      syncPane(from, to, expected)
    }

    // 滚动事件高频：一帧只同步一次，避免一次滚动里反复查页坐标
    const onLeft = () => {
      if (frameLeft || !boundLeft || !boundRight) return
      frameLeft = window.requestAnimationFrame(() => {
        frameLeft = 0
        if (boundLeft && boundRight) follow(boundLeft, boundRight)
      })
    }
    const onRight = () => {
      if (frameRight || !boundLeft || !boundRight) return
      frameRight = window.requestAnimationFrame(() => {
        frameRight = 0
        if (boundLeft && boundRight) follow(boundRight, boundLeft)
      })
    }

    // 右栏 PDF 页面可能还没量出高度：短间隔重试，直到能算出对侧位置
    const align = (attempt = 0) => {
      if (!boundLeft || !boundRight) return
      const from = driver === boundRight ? boundRight : boundLeft
      const to = from === boundLeft ? boundRight : boundLeft
      if (syncPane(from, to, expected) || attempt >= 6) return
      timers.push(window.setTimeout(() => align(attempt + 1), 200))
    }
    const alignSoon = () => {
      // 用户正在滚动时不要用尺寸变化回调抢方向：否则从右栏滚动会被左栏覆盖
      if (performance.now() - lastUserScroll < 350) return
      if (alignFrame) return
      alignFrame = window.requestAnimationFrame(() => {
        alignFrame = 0
        align()
      })
    }

    /**
     * 绑定左右滚动容器。译文栏会先渲染骨架、随后被真正的 pdf.js 页面替换，
     * 节点一变监听就失效；因此用 MutationObserver 重新绑定，而不是只依赖一次 effect。
     */
    const bind = () => {
      const left = root.querySelector<HTMLElement>('.dual-pane-pdf .pdf-view')
      const right = root.querySelector<HTMLElement>('.dual-pane-zh .pdf-view')
      if (!left || !right) return
      if (left === boundLeft && right === boundRight) return
      if (boundLeft) boundLeft.removeEventListener('scroll', onLeft)
      if (boundRight) boundRight.removeEventListener('scroll', onRight)
      resizeObserver?.disconnect()
      boundLeft = left
      boundRight = right
      left.addEventListener('scroll', onLeft, { passive: true })
      right.addEventListener('scroll', onRight, { passive: true })
      // 页宽/页高变化（缩放、拖动分隔条、译文页渲染完成）后重新对齐一次
      resizeObserver = new ResizeObserver(alignSoon)
      const leftPages = left.querySelector('.pdf-pages')
      const rightPages = right.querySelector('.pdf-pages')
      if (leftPages) resizeObserver.observe(leftPages)
      if (rightPages) resizeObserver.observe(rightPages)
      align()
    }

    watcher = new MutationObserver(bind)
    watcher.observe(root, { childList: true, subtree: true })
    bind()

    return () => {
      watcher?.disconnect()
      resizeObserver?.disconnect()
      if (frameLeft) window.cancelAnimationFrame(frameLeft)
      if (frameRight) window.cancelAnimationFrame(frameRight)
      if (alignFrame) window.cancelAnimationFrame(alignFrame)
      timers.forEach((id) => window.clearTimeout(id))
      if (boundLeft) boundLeft.removeEventListener('scroll', onLeft)
      if (boundRight) boundRight.removeEventListener('scroll', onRight)
    }
  }, [showSource, syncEnabled, translationReady])

  // 缩放：两栏各自按倍率换算出新的滚动位置（保持同一视野），再把右栏对齐到左栏
  useLayoutEffect(() => {
    const previous = prevZoom.current
    prevZoom.current = zoom
    if (previous === zoom || previous <= 0) return
    const root = document.querySelector('.dual-reader')
    const left = root?.querySelector<HTMLElement>('.dual-pane-pdf .pdf-view')
    const right = root?.querySelector<HTMLElement>('.dual-pane-zh .pdf-view')
    const panes = [left, right].filter((el): el is HTMLElement => Boolean(el))
    if (!panes.length) return
    const factor = zoom / previous
    const expected = expectedPos.current
    for (const pane of panes) {
      const maxTop = Math.max(0, pane.scrollHeight - pane.clientHeight)
      const maxLeft = Math.max(0, pane.scrollWidth - pane.clientWidth)
      const top = Math.min(maxTop, Math.max(0, pane.scrollTop * factor))
      const leftPos = Math.min(maxLeft, Math.max(0, pane.scrollLeft * factor))
      if (Math.abs(top - pane.scrollTop) < 0.5 && Math.abs(leftPos - pane.scrollLeft) < 0.5) {
        continue
      }
      pane.scrollTop = top
      pane.scrollLeft = leftPos
      expected.set(pane, { top: pane.scrollTop, left: pane.scrollLeft })
    }
    if (!showSource || !syncEnabled || !left || !right) return
    // pdf.js 重新渲染帧尺寸要晚一两帧才稳定，缩放后补两次对齐
    const timers = [
      window.setTimeout(() => syncPane(left, right, expected), 120),
      window.setTimeout(() => syncPane(left, right, expected), 420),
    ]
    return () => timers.forEach((id) => window.clearTimeout(id))
  }, [zoom, showSource, syncEnabled])

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