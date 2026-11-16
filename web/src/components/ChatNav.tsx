import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'

/** 一轮问答在导航里只保留提问开头的这句摘要。 */
export interface ChatNavItem {
  id: string
  text: string
}

const SUMMARY_LIMIT = 64
/** 超过这个轮次才显示导航：短对话没必要占位。 */
const SHOW_THRESHOLD = 3

function clipSummary(text: string): string {
  const clean = text.replace(/\s+/g, ' ').trim()
  if (clean.length <= SUMMARY_LIMIT) return clean
  return `${clean.slice(0, SUMMARY_LIMIT)}…`
}

/** 界面整体带 `html { zoom }`：鼠标坐标是缩放后的视口像素，需要换算回 CSS 像素。 */
function zoomFactor(): number {
  const raw = (getComputedStyle(document.documentElement) as unknown as {
    zoom?: string
  }).zoom
  const value = Number(raw)
  return Number.isFinite(value) && value > 0 ? value : 1
}

function escapeId(value: string): string {
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
    return CSS.escape(value)
  }
  return value.replace(/["\\]/g, '\\$&')
}

/** 滚动容器自身可能被 zoom 放大：rect 高度 / offsetHeight 就是真实倍率。 */
function zoomOfBox(box: HTMLElement): number {
  const height = box.offsetHeight
  if (!height) return zoomFactor()
  const ratio = box.getBoundingClientRect().height / height
  return Number.isFinite(ratio) && ratio > 0 ? ratio : zoomFactor()
}

interface TipState {
  id: string
  text: string
  x: number
  y: number
}

export function ChatNav({
  items,
  scrollRef,
  contentSelector,
  threshold = SHOW_THRESHOLD,
}: {
  items: ChatNavItem[]
  /** 面板自身的滚动容器 */
  scrollRef: RefObject<HTMLElement | null>
  /** 居中内容容器：导航贴在它左侧，不随容器宽度漂移 */
  contentSelector?: string
  threshold?: number
}) {
  const [activeId, setActiveId] = useState<string | null>(items[0]?.id ?? null)
  const [left, setLeft] = useState(8)
  const [tip, setTip] = useState<TipState | null>(null)
  const frame = useRef<number | null>(null)

  const syncActive = useCallback(() => {
    const box = scrollRef.current
    if (!box) return
    const nodes = Array.from(
      box.querySelectorAll<HTMLElement>('[data-chat-nav-item]'),
    )
    if (nodes.length === 0) return
    const boxTop = box.getBoundingClientRect().top
    const limit = 52 * zoomOfBox(box)
    let current = nodes[0]
    for (const node of nodes) {
      if (node.getBoundingClientRect().top - boxTop <= limit) current = node
    }
    const id = current.dataset.chatNavItem
    if (id) setActiveId(id)
  }, [scrollRef])

  // 贴内容左边缘：宽屏时跟着居中内容走，窄屏时贴容器左侧
  useLayoutEffect(() => {
    const box = scrollRef.current
    if (!box) return
    const measure = () => {
      if (!contentSelector) {
        setLeft(8)
        return
      }
      const content = box.querySelector<HTMLElement>(contentSelector)
      if (!content) {
        setLeft(6)
        return
      }
      const gutter = (box.clientWidth - content.offsetWidth) / 2
      setLeft(Math.max(6, Math.round(gutter - 16)))
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(box)
    const content = contentSelector
      ? box.querySelector<HTMLElement>(contentSelector)
      : null
    if (content) observer.observe(content)
    return () => observer.disconnect()
  }, [scrollRef, contentSelector])

  // 滚动与轮次变化都要重新判定当前节点
  useEffect(() => {
    const box = scrollRef.current
    if (!box) return
    const request = () => {
      if (frame.current !== null) return
      frame.current = window.requestAnimationFrame(() => {
        frame.current = null
        syncActive()
      })
    }
    request()
    box.addEventListener('scroll', request, { passive: true })
    const observer = new ResizeObserver(request)
    observer.observe(box)
    return () => {
      box.removeEventListener('scroll', request)
      observer.disconnect()
      if (frame.current !== null) {
        window.cancelAnimationFrame(frame.current)
        frame.current = null
      }
    }
  }, [scrollRef, syncActive, items.length])

  useEffect(() => {
    if (activeId && items.some((item) => item.id === activeId)) return
    setActiveId(items[0]?.id ?? null)
  }, [items, activeId])

  // 鼠标移出导航区域时兜底收起提示，避免滚动后残留
  useEffect(() => {
    if (!tip) return
    const hide = () => setTip(null)
    window.addEventListener('scroll', hide, true)
    return () => window.removeEventListener('scroll', hide, true)
  }, [tip])

  const jump = (id: string) => {
    const box = scrollRef.current
    if (!box) return
    const node = box.querySelector<HTMLElement>(
      `[data-chat-nav-item="${escapeId(id)}"]`,
    )
    if (!node) return
    const boxRect = box.getBoundingClientRect()
    const nodeRect = node.getBoundingClientRect()
    const delta = (nodeRect.top - boxRect.top) / zoomOfBox(box) - 12
    setActiveId(id)
    box.scrollTo({ top: box.scrollTop + delta, behavior: 'smooth' })
  }

  if (items.length <= threshold) return null

  const tipViewport = tip
    ? (() => {
        const zoom = zoomFactor()
        const x = tip.x / zoom
        const y = tip.y / zoom
        const width = window.innerWidth / zoom
        const height = window.innerHeight / zoom
        return {
          left: Math.min(Math.max(8, x + 14), Math.max(8, width - 252)),
          top: Math.min(Math.max(8, y + 16), Math.max(8, height - 76)),
        }
      })()
    : null

  return (
    <>
      <div className="chat-nav" style={{ left }} role="navigation" aria-label="问答节点">
        {items.map((item, index) => (
          <button
            key={item.id}
            type="button"
            className="chat-nav-dot"
            data-active={item.id === activeId ? 'true' : 'false'}
            aria-label={`第 ${index + 1} 轮：${clipSummary(item.text)}`}
            aria-current={item.id === activeId ? 'true' : undefined}
            onClick={() => jump(item.id)}
            onMouseEnter={(event) =>
              setTip({
                id: item.id,
                text: item.text,
                x: event.clientX,
                y: event.clientY,
              })
            }
            onMouseMove={(event) =>
              setTip((prev) =>
                prev && prev.id === item.id
                  ? { ...prev, x: event.clientX, y: event.clientY }
                  : prev,
              )
            }
            onMouseLeave={() => setTip(null)}
          />
        ))}
      </div>
      {tip &&
        tipViewport &&
        createPortal(
          <div className="chat-nav-tip" style={tipViewport} role="tooltip">
            {clipSummary(tip.text)}
          </div>,
          document.body,
        )}
    </>
  )
}