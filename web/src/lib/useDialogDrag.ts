import { useCallback, useEffect, useRef, useState } from 'react'
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react'

/**
 * 弹窗拖动：让所有居中卡片弹窗都能按住标题栏或空白处移动。
 *
 * 约束（与阅读页浮层一致）：
 * - 位移超过 4px 才进入拖拽，避免把点击误判成拖动；
 * - 拖拽结束那一次 click 被抑制，不会误触按钮或关闭弹窗；
 * - 输入框、按钮、链接、可编辑区域与正文文字不抢拖动，正文仍可正常选中复制；
 * - 全站 `html { zoom: 1.2 }`，鼠标位移与视口边界都要先按缩放系数换算；
 * - 卡片始终限制在视口内，窗口缩小后重新收敛。
 */
const NO_DRAG_SELECTOR =
  'input, textarea, select, button, a, label, iframe, video, audio, [contenteditable="true"], [data-no-drag]'
const TEXT_SELECTOR = 'p, li, pre, code, h1, h2, h3, h4, h5, h6, span, em, strong, small, th, td'
const HANDLE_SELECTOR = '[data-drag-handle]'
const DRAG_THRESHOLD = 4
const VIEWPORT_MARGIN = 8

function zoomFactor(): number {
  const raw = getComputedStyle(document.documentElement).zoom
  const value = Number.parseFloat(raw)
  return Number.isFinite(value) && value > 0 ? value : 1
}

function clampAxis(value: number, min: number, max: number): number {
  // 卡片比视口还大时无法完全收进，居中放置，至少保证两侧边缘对称可达。
  if (max < min) return (min + max) / 2
  return Math.min(Math.max(value, min), max)
}

interface DragState {
  pointerId: number
  startX: number
  startY: number
  originX: number
  originY: number
  baseLeft: number
  baseTop: number
  width: number
  height: number
  moved: boolean
}

interface Offset {
  x: number
  y: number
}

function measure(card: HTMLElement, offset: Offset) {
  const zoom = zoomFactor()
  const rect = card.getBoundingClientRect()
  return {
    zoom,
    baseLeft: rect.left / zoom - offset.x,
    baseTop: rect.top / zoom - offset.y,
    width: rect.width / zoom,
    height: rect.height / zoom,
  }
}

function clampOffset(
  base: { baseLeft: number; baseTop: number; width: number; height: number },
  zoom: number,
  x: number,
  y: number,
): Offset {
  const viewportWidth = window.innerWidth / zoom
  const viewportHeight = window.innerHeight / zoom
  const minX = VIEWPORT_MARGIN - base.baseLeft
  const maxX = viewportWidth - VIEWPORT_MARGIN - (base.baseLeft + base.width)
  const minY = VIEWPORT_MARGIN - base.baseTop
  const maxY = viewportHeight - VIEWPORT_MARGIN - (base.baseTop + base.height)
  return {
    x: clampAxis(x, minX, maxX),
    y: clampAxis(y, minY, maxY),
  }
}

export interface DialogDragProps {
  'data-dialog-drag': 'idle' | 'dragging'
  style: CSSProperties
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void
  onPointerMove: (event: ReactPointerEvent<HTMLElement>) => void
  onPointerUp: (event: ReactPointerEvent<HTMLElement>) => void
  onPointerCancel: (event: ReactPointerEvent<HTMLElement>) => void
  onClickCapture: (event: React.MouseEvent<HTMLElement>) => void
}

export function useDialogDrag<T extends HTMLElement = HTMLDivElement>() {
  const ref = useRef<T | null>(null)
  const [offset, setOffset] = useState<Offset>({ x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  const offsetRef = useRef(offset)
  offsetRef.current = offset
  const dragRef = useRef<DragState | null>(null)
  const suppressClick = useRef(false)

  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) return
    const card = ref.current
    const target = event.target as Element | null
    if (!card || !target) return

    if (target.closest('[data-no-drag]')) return
    const fromHandle = Boolean(target.closest(HANDLE_SELECTOR))
    if (!fromHandle && target.closest(NO_DRAG_SELECTOR)) return
    if (!fromHandle && target.closest(TEXT_SELECTOR)) return

    const current = offsetRef.current
    const base = measure(card, current)
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: current.x,
      originY: current.y,
      ...base,
      moved: false,
    }
    try {
      card.setPointerCapture(event.pointerId)
    } catch {
      /* 指针捕获失败时仍可依赖冒泡移动 */
    }
  }, [])

  const onPointerMove = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return

    const zoom = zoomFactor()
    const dx = (event.clientX - drag.startX) / zoom
    const dy = (event.clientY - drag.startY) / zoom

    if (!drag.moved) {
      if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return
      drag.moved = true
      setDragging(true)
      document.body.classList.add('is-dialog-dragging')
    }

    const next = clampOffset(drag, zoom, drag.originX + dx, drag.originY + dy)
    setOffset(next)
  }, [])

  const finish = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    if (drag.moved) suppressClick.current = true
    dragRef.current = null
    setDragging(false)
    document.body.classList.remove('is-dialog-dragging')
    const card = ref.current
    try {
      if (card?.hasPointerCapture(event.pointerId)) card.releasePointerCapture(event.pointerId)
    } catch {
      /* 已释放 */
    }
  }, [])

  const onClickCapture = useCallback((event: React.MouseEvent<HTMLElement>) => {
    if (!suppressClick.current) return
    suppressClick.current = false
    event.preventDefault()
    event.stopPropagation()
  }, [])

  // 窗口尺寸变化时把已经拖出去的卡片收回视口。
  useEffect(() => {
    const onResize = () => {
      const card = ref.current
      if (!card) return
      const current = offsetRef.current
      if (current.x === 0 && current.y === 0) return
      const base = measure(card, current)
      setOffset(clampOffset(base, base.zoom, current.x, current.y))
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      document.body.classList.remove('is-dialog-dragging')
    }
  }, [])

  const props: DialogDragProps = {
    'data-dialog-drag': dragging ? 'dragging' : 'idle',
    style: {
      transform: `translate3d(${offset.x}px, ${offset.y}px, 0)`,
      willChange: 'transform',
    },
    onPointerDown,
    onPointerMove,
    onPointerUp: finish,
    onPointerCancel: finish,
    onClickCapture,
  }

  return { ref, props, dragging, reset: () => setOffset({ x: 0, y: 0 }) }
}