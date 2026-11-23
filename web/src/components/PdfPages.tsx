import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'react-pdf/dist/Page/TextLayer.css'
import { mediaUrl } from '../lib/mobile'
import type { ReaderQuoteDraft } from '../types'
import type {
  Annotation,
  AnnotationColor,
  AnnotationKind,
  PdfRect,
} from '../lib/library'
import type { AnnotationActions } from './Annotations'
import { AnnotationPopover, SelectionToolbar } from './Annotations'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

interface Props {
  paperId: string
  pageCount: number
  /** 每页 [宽, 高]（PDF 点）；用来给页图占位，跳页时不会因为图片没加载而错位 */
  pageSizes?: [number, number][]
  /** 切到原版时停留的页码（1 起），进来后直接滚到这一页 */
  initialPage: number
  /** 页面显示宽度（px）；图片按固定分辨率取，缩放只改 CSS 宽度 */
  width: number
  /**
   * 页面放大倍率（1 = 适配当前宽度）。大于 1 时页宽超出容器、可横向滚动，
   * 双栏对比用它做「放大到只显示页面一部分」的局部放大。
   */
  zoom?: number
  /** 面板里点目录/重点/图表时滚到对应页；token 变化即触发。双栏不传，交给滚动联动 */
  jump?: { page: number; token: number } | null
  onPageChange: (page: number) => void
  /** 离线页图（页码 → blob URL）；有值时不再请求服务器 */
  pageUrls?: Record<number, string>
  /** PDF 文件地址；有值时用 pdf.js 文本层渲染，可在原文上划词批注 */
  pdfUrl?: string | null
  annotations?: Annotation[]
  annotationActions?: AnnotationActions
  /** 双击页面上已有的荧光笔/批注：左侧批注栏切到并定位到这一条 */
  onOpenAnnotation?: (annotation: Annotation) => void
  /** 划词工具条点「提问」：把原版 PDF 里选中的文字带到左侧问答面板 */
  onAskQuote?: (quote: ReaderQuoteDraft) => void
  /** 审阅模式：在每一页右侧按位置渲染批注卡片（类似 Word 的批注气泡） */
  reviewMode?: boolean
  /** 双击标注后聚焦：右侧对应卡片滚动到视野并闪两下 */
  focusAnnotation?: { id: string; at: number } | null
}

interface PdfSelection {
  page: number
  quote: string
  rects: PdfRect[]
  /** 选区在视口里的位置，用于浮出工具条 */
  rect: DOMRect
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value))
}

function normalizeRect(rect: DOMRect, frame: DOMRect): PdfRect {
  const x = clamp01((rect.left - frame.left) / Math.max(1, frame.width))
  const y = clamp01((rect.top - frame.top) / Math.max(1, frame.height))
  return {
    x,
    y,
    w: clamp01(rect.width / Math.max(1, frame.width)),
    h: clamp01(rect.height / Math.max(1, frame.height)),
  }
}

const RECT_EPS = 0.0015

/**
 * 把一次选区拆出的矩形合并成「每行一个」。
 *
 * pdf.js 文本层是按 span 排的，`range.getClientRects()` 在同一行会返回多个
 * 互相重叠的小矩形；逐个画半透明色会叠成实色并遮住页面文字，逐个画下划线
 * 则同一行出现两条甚至更多线。先去掉重复框，再把同一行的框横向并成一段，
 * 荧光笔是连续的一条、批注是干净的一条下划线。
 */
function mergeLineRects(rects: PdfRect[]): PdfRect[] {
  if (rects.length <= 1) return rects.map((rect) => ({ ...rect }))
  const unique: PdfRect[] = []
  for (const rect of rects) {
    const duplicate = unique.some(
      (item) =>
        Math.abs(item.x - rect.x) < RECT_EPS &&
        Math.abs(item.y - rect.y) < RECT_EPS &&
        Math.abs(item.w - rect.w) < RECT_EPS &&
        Math.abs(item.h - rect.h) < RECT_EPS,
    )
    if (!duplicate) unique.push({ ...rect })
  }
  const lines: PdfRect[] = []
  for (const rect of unique.sort((a, b) => a.y - b.y || a.x - b.x)) {
    const line = lines.find(
      (item) => Math.abs(item.y - rect.y) <= Math.max(item.h, rect.h) * 0.45,
    )
    if (!line) {
      lines.push({ ...rect })
      continue
    }
    const left = Math.min(line.x, rect.x)
    const right = Math.min(1, Math.max(line.x + line.w, rect.x + rect.w))
    const top = Math.min(line.y, rect.y)
    const bottom = Math.min(1, Math.max(line.y + line.h, rect.y + rect.h))
    line.x = left
    line.y = top
    line.w = Math.max(0, right - left)
    line.h = Math.max(0, bottom - top)
  }
  return lines
}

/** 页内标注层：按归一化矩形画荧光笔填色或彩色下划线。 */
function PdfAnnotationLayer({
  page,
  annotations,
}: {
  page: number
  annotations: Annotation[]
}) {
  const items = annotations.filter((item) => item.page === page && item.rects?.length)
  if (!items.length) return null
  // 同页同类型同颜色的标注合并成一簇：重复划同一段文字时，两条半透明矩形
  // 完全重合会叠成一条更深的框（肉眼就是「上下两层框」），合并后只画一次。
  const groups = new Map<string, { kind: string; color: string; rects: PdfRect[] }>()
  for (const item of items) {
    const key = `${item.kind}:${item.color}`
    const group = groups.get(key) ?? { kind: item.kind, color: item.color, rects: [] }
    group.rects.push(...mergeLineRects(item.rects ?? []))
    groups.set(key, group)
  }
  return (
    <div className="pdf-annot-layer" aria-hidden>
      {[...groups.entries()].flatMap(([key, group]) =>
        mergeOverlaps(group.rects).map((rect, index) => (
          <span
            key={`${key}-${index}`}
            className={`pdf-annot pdf-annot-${group.kind} pdf-annot-${group.color}`}
            style={{
              left: `${rect.x * 100}%`,
              top: `${rect.y * 100}%`,
              width: `${rect.w * 100}%`,
              height: `${rect.h * 100}%`,
            }}
          />
        )),
      )}
    </div>
  )
}

/**
 * 把同色跨标注的重叠矩形并成一段。
 *
 * 只合并真正相交（同一行且横向有交集）的框，中间留白不填色，因此不会把
 * 同一行里两处分开的荧光笔连成一条；完全重合的重复标注只保留一个。
 */
function mergeOverlaps(rects: PdfRect[]): PdfRect[] {
  const out: PdfRect[] = []
  for (const rect of rects) {
    let merged = false
    for (const line of out) {
      const sameLine = Math.abs(line.y - rect.y) <= Math.max(line.h, rect.h) * 0.45
      const overlapsX = rect.x < line.x + line.w - RECT_EPS && rect.x + rect.w > line.x + RECT_EPS
      if (!sameLine || !overlapsX) continue
      const left = Math.min(line.x, rect.x)
      const right = Math.min(1, Math.max(line.x + line.w, rect.x + rect.w))
      const top = Math.min(line.y, rect.y)
      const bottom = Math.min(1, Math.max(line.y + line.h, rect.y + rect.h))
      line.x = left
      line.y = top
      line.w = Math.max(0, right - left)
      line.h = Math.max(0, bottom - top)
      merged = true
      break
    }
    if (!merged) out.push({ ...rect })
  }
  return out
}

// 服务端按这个宽度渲染，前端只做等比缩放；再大对公式也没有额外增益。
const RENDER_WIDTH = 1200

/** 审阅模式右侧批注栏宽度与卡片间距（布局像素）。 */
const REVIEW_WIDTH = 280
const REVIEW_GAP = 16
const CARD_GAP = 8

const NOTE_TYPE_LABEL: Record<Annotation['kind'], string> = {
  highlight: '荧光笔',
  comment: '批注',
}

/**
 * 审阅模式：在一页右侧按批注在页面里的纵向位置渲染卡片。
 *
 * 卡片默认绝对定位、按 rects[0].y 对到页面对应行，互相重叠时向下让位；
 * 一页批注太多、整体超出页面高度时整列上移，避免压到下一页。
 * 可用宽度不足时（stacked）改为在页面下方按顺序排列。
 */
function PdfMarginNotes({
  page,
  annotations,
  frameHeight,
  stacked,
  actions,
  focusAnnotation,
}: {
  page: number
  annotations: Annotation[]
  frameHeight: number
  stacked: boolean
  actions?: AnnotationActions
  focusAnnotation?: { id: string; at: number } | null
}) {
  const items = useMemo(
    () =>
      annotations
        .filter((item) => item.page === page && item.rects?.length)
        .sort(
          (a, b) =>
            (a.rects?.[0]?.y ?? 0) - (b.rects?.[0]?.y ?? 0) ||
            a.created_at - b.created_at,
        ),
    [annotations, page],
  )
  const cardRefs = useRef(new Map<string, HTMLElement>())
  const [tops, setTops] = useState<Record<string, number>>({})
  const [contentHeight, setContentHeight] = useState(frameHeight)

  useLayoutEffect(() => {
    if (stacked) {
      setTops({})
      setContentHeight(frameHeight)
      return
    }
    const next: Record<string, number> = {}
    let cursor = 0
    for (const item of items) {
      const height = cardRefs.current.get(item.id)?.offsetHeight ?? 96
      const desired = Math.max(0, (item.rects?.[0]?.y ?? 0) * frameHeight - 14)
      const top = Math.max(desired, cursor)
      next[item.id] = top
      cursor = top + height + CARD_GAP
    }
    // 卡片整体超过页面高度时整列上移，优先保证第一条贴近原位置
    const bottom = items.length ? cursor - CARD_GAP : 0
    const overflow = bottom - frameHeight
    if (overflow > 0 && items.length) {
      const firstId = items[0].id
      const shift = Math.min(overflow, next[firstId] ?? 0)
      if (shift > 0) {
        for (const key of Object.keys(next)) next[key] -= shift
      }
    }
    setTops(next)
    setContentHeight(Math.max(frameHeight, bottom))
  }, [items, frameHeight, stacked])

  useEffect(() => {
    if (!focusAnnotation) return
    const el = cardRefs.current.get(focusAnnotation.id)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.classList.remove('is-flash')
    void el.offsetWidth
    el.classList.add('is-flash')
    const timer = window.setTimeout(() => el.classList.remove('is-flash'), 1500)
    return () => {
      window.clearTimeout(timer)
      el.classList.remove('is-flash')
    }
  }, [focusAnnotation])

  return (
    <aside
      className={`pdf-margin-notes${stacked ? ' is-stacked' : ''}`}
      aria-label={`第 ${page} 页批注`}
      style={
        stacked
          ? undefined
          : { height: `${Math.max(frameHeight, contentHeight)}px` }
      }
    >
      {items.map((item, index) => {
        const top = tops[item.id]
        return (
          <article
            className="pdf-margin-card"
            key={`${item.id}-${item.updated_at}`}
            data-annotation-id={item.id}
            data-color={item.color}
            ref={(node) => {
              if (node) cardRefs.current.set(item.id, node)
              else cardRefs.current.delete(item.id)
            }}
            style={
              stacked
                ? undefined
                : {
                    top: `${top ?? 0}px`,
                    visibility: top === undefined ? 'hidden' : undefined,
                  }
            }
          >
            <header className="pdf-margin-head">
              <span className="pdf-margin-index">{index + 1}</span>
              <span
                className={`note-swatch note-${item.kind} note-${item.kind}-${item.color}`}
                aria-hidden
              />
              <span className="pdf-margin-type">{NOTE_TYPE_LABEL[item.kind]}</span>
              {!stacked && <span className="pdf-margin-page">P{page}</span>}
              {actions && (
                <button
                  className="link-btn danger pdf-margin-delete"
                  type="button"
                  title="删除这条批注"
                  onClick={() => {
                    if (window.confirm(`删除这条${NOTE_TYPE_LABEL[item.kind]}？`)) {
                      void actions.remove(item.id)
                    }
                  }}
                >
                  删除
                </button>
              )}
            </header>
            <p className="pdf-margin-quote">{item.quote}</p>
            <textarea
              className="note-input pdf-margin-input"
              defaultValue={item.note}
              placeholder={
                item.kind === 'comment' ? '写下你的批注…' : '给这处荧光笔补一句笔记…'
              }
              rows={2}
              disabled={!actions}
              onBlur={(event) => {
                const next = event.target.value
                if (actions && next !== item.note) {
                  void actions.update(item.id, { note: next })
                }
              }}
            />
          </article>
        )
      })}
    </aside>
  )
}

/**
 * 原版 PDF 视图：逐页竖排、连续滚动。
 *
 * 有 PDF 文件时用 pdf.js 渲染页面并叠加透明文本层，文字可以像普通网页一样选中，
 * 选中后能直接划荧光笔 / 写批注；PDF 不可用（离线包只有页图）时回退到服务端页图，
 * 保证任何环境都能看论文。
 */
export function PdfPages({
  paperId,
  pageCount,
  pageSizes,
  initialPage,
  width,
  zoom = 1,
  jump = null,
  onPageChange,
  pageUrls,
  pdfUrl,
  annotations = [],
  annotationActions,
  onOpenAnnotation,
  onAskQuote,
  reviewMode = false,
  focusAnnotation,
}: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const jumped = useRef(false)
  const total = Math.max(1, pageCount)
  const [pdfFailed, setPdfFailed] = useState(false)
  // react-pdf 在 PDF 加载完成前不会渲染 children；观察器必须等页面节点真正出现后再注册，
  // 否则一次空查询后就再也不会重跑，表现为只能看到前两页。
  const [pdfReady, setPdfReady] = useState(false)
  const [available, setAvailable] = useState(0)
  const [visible, setVisible] = useState<Set<number>>(
    () => new Set([initialPage - 1, initialPage, initialPage + 1]),
  )
  const [selection, setSelection] = useState<PdfSelection | null>(null)
  const [editing, setEditing] = useState<{
    annotation: Annotation
    rect: DOMRect
    /** 单击已有标注的查看态：不自动聚焦输入 */
    peek?: boolean
  } | null>(null)
  // 单击标注与双击（切批注栏）共用同一次点击序列，靠这个延时区分两者
  const peekTimer = useRef<number | null>(null)
  // 阅读区太窄时，右侧批注栏改到页面下方，避免把页面挤得过窄
  const [stackedNotes, setStackedNotes] = useState(false)
  const useTextLayer = Boolean(pdfUrl) && !pdfFailed

  // 适配宽度 = min(基准宽度, 本栏可用宽度)；放大时在此基础上乘倍率，允许超出容器横向滚动
  const fitWidth = Math.max(320, Math.min(width, available || width))
  const pageWidth = zoom > 1 ? Math.max(320, Math.round(fitWidth * zoom)) : fitWidth
  const pagesClass = `pdf-pages${zoom > 1 ? ' is-zoomed' : ''}`

  const scrollToPage = useCallback((page: number) => {
    scrollRef.current
      ?.querySelector<HTMLElement>(`[data-page="${page}"]`)
      ?.scrollIntoView({ block: 'start' })
  }, [])

  useEffect(() => {
    return () => {
      if (peekTimer.current) window.clearTimeout(peekTimer.current)
    }
  }, [])

  // 图片还没加载完时容器高度是 0，先滚一次，再在目标页 onLoad 时补一次
  useEffect(() => {
    jumped.current = false
    const timer = window.setTimeout(() => {
      if (jumped.current) return
      jumped.current = true
      scrollToPage(initialPage)
    }, 300)
    return () => window.clearTimeout(timer)
  }, [initialPage, paperId, scrollToPage])

  // 左侧面板点目录 / 重点 / 图表：滚到对应页
  useEffect(() => {
    if (!jump) return
    scrollToPage(jump.page)
    setVisible((prev) => {
      const next = new Set(prev)
      next.add(jump.page)
      return next
    })
  }, [jump, scrollToPage])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      const top = el.getBoundingClientRect().top + 120
      let current = 1
      el.querySelectorAll<HTMLElement>('[data-page]').forEach((node) => {
        if (node.getBoundingClientRect().top <= top) current = Number(node.dataset.page)
      })
      onPageChange(current)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => el.removeEventListener('scroll', onScroll)
  }, [onPageChange])

  // 测量可用宽度：页宽不能超过阅读区，否则文本层会被裁掉。
  // 审阅模式下还要给右侧批注栏留出固定宽度（阅读区太窄时卡片改到页面下方）。
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const measure = () => {
      const stacked = el.clientWidth < REVIEW_WIDTH + 536
      setStackedNotes(stacked)
      const notesWidth = reviewMode && !stacked ? REVIEW_WIDTH + REVIEW_GAP : 0
      setAvailable(Math.max(320, el.clientWidth - 56 - notesWidth))
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [reviewMode])

  // 只渲染视口附近的页：整本几百页也不会一次性建 DOM
  useEffect(() => {
    if (!useTextLayer) return
    const root = scrollRef.current
    if (!root) return
    const targets = root.querySelectorAll<HTMLElement>('[data-page]')
    if (targets.length === 0) return
    const observer = new IntersectionObserver(
      (entries) => {
        setVisible((prev) => {
          let changed = false
          const next = new Set(prev)
          for (const entry of entries) {
            const page = Number((entry.target as HTMLElement).dataset.page)
            if (!page) continue
            if (entry.isIntersecting) {
              if (!next.has(page)) {
                next.add(page)
                changed = true
              }
            } else if (next.delete(page)) {
              changed = true
            }
          }
          return changed ? next : prev
        })
      },
      { root, rootMargin: '900px 0px' },
    )
    targets.forEach((node) => observer.observe(node))
    return () => observer.disconnect()
  }, [useTextLayer, total, pdfReady])

  const onFrameMouseUp = (event: ReactMouseEvent<HTMLDivElement>) => {
    // 双击的第二下用于「跳到批注」，不再触发划词工具条
    if (event.detail > 1) return
    const frame = event.currentTarget
    window.setTimeout(() => {
      const sel = window.getSelection()
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) return
      const range = sel.getRangeAt(0)
      if (!frame.contains(range.startContainer) || !frame.contains(range.endContainer)) return
      const quote = sel.toString().replace(/\s+/g, ' ').trim()
      if (!quote) return
      const page = Number(
        frame.closest<HTMLElement>('[data-page]')?.dataset.page || 0,
      )
      if (!page) return
      const frameRect = frame.getBoundingClientRect()
      const rects = mergeLineRects(
        [...range.getClientRects()]
          .filter((rect) => rect.width > 1 && rect.height > 1)
          .map((rect) => normalizeRect(rect, frameRect))
          .filter((rect) => rect.w > 0 && rect.h > 0),
      )
      if (!rects.length) return
      setSelection({ page, quote, rects, rect: range.getBoundingClientRect() })
    }, 0)
  }

  /**
   * 按页面归一化坐标找鼠标下面的标注；rect 用于把编辑框锚在点击的那一行旁边。
   */
  const hitAnnotationAt = (event: ReactMouseEvent<HTMLDivElement>) => {
    const frame = event.currentTarget
    const bounds = frame.getBoundingClientRect()
    if (!bounds.width || !bounds.height) return null
    const page = Number(frame.closest<HTMLElement>('[data-page]')?.dataset.page || 0)
    if (!page) return null
    const x = (event.clientX - bounds.left) / bounds.width
    const y = (event.clientY - bounds.top) / bounds.height
    if (x < 0 || x > 1 || y < 0 || y > 1) return null
    const pad = 0.006
    const hits = annotations.filter(
      (item) =>
        item.page === page &&
        (item.rects ?? []).some(
          (rect) =>
            x >= rect.x - pad &&
            x <= rect.x + rect.w + pad &&
            y >= rect.y - pad &&
            y <= rect.y + rect.h + pad,
        ),
    )
    if (!hits.length) return null
    // 后创建的标注画在上层，命中时优先返回它
    const hit = [...hits].sort((a, b) =>
      String(a.created_at ?? '').localeCompare(String(b.created_at ?? '')),
    )[hits.length - 1]
    const rects = hit.rects ?? []
    const rect =
      rects.find(
        (item) =>
          x >= item.x - pad &&
          x <= item.x + item.w + pad &&
          y >= item.y - pad &&
          y <= item.y + item.h + pad,
      ) ?? rects[0]
    if (!rect) return null
    return {
      annotation: hit,
      rect: new DOMRect(
        bounds.left + rect.x * bounds.width,
        bounds.top + rect.y * bounds.height,
        Math.max(1, rect.w * bounds.width),
        Math.max(1, rect.h * bounds.height),
      ),
    }
  }

  /**
   * 单击已有的荧光笔/批注：在标注旁边直接弹出笔记小窗（不切左侧栏）。
   * 双击仍走原逻辑切到批注栏，所以这里延时后再弹，双击会被取消。
   */
  const onFrameClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (reviewMode || !annotationActions || event.button !== 0) return
    const sel = window.getSelection()
    if (sel && !sel.isCollapsed && sel.toString().trim()) return
    const hit = hitAnnotationAt(event)
    if (!hit) return
    if (peekTimer.current) window.clearTimeout(peekTimer.current)
    peekTimer.current = window.setTimeout(() => {
      peekTimer.current = null
      setSelection(null)
      setEditing({ annotation: hit.annotation, rect: hit.rect, peek: true })
    }, 240)
  }

  /**
   * 双击页面上的荧光笔/批注：按坐标命中已有的标注矩形，交给左侧批注栏定位。
   *
   * 标注层本身是 `pointer-events: none`（否则会挡住文本选择），所以这里不改
   * 图层的事件，而是用归一化坐标反查——既能双击定位，也不影响正常划词。
   */
  const onFrameDoubleClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (peekTimer.current) {
      window.clearTimeout(peekTimer.current)
      peekTimer.current = null
    }
    // 双击语义是「去批注栏继续看」，不叠加行内小窗
    setEditing(null)
    if (!onOpenAnnotation) return
    const hit = hitAnnotationAt(event)
    if (!hit) return
    event.preventDefault()
    event.stopPropagation()
    window.getSelection()?.removeAllRanges()
    setSelection(null)
    onOpenAnnotation(hit.annotation)
  }

  const applyAnnotation = async (color: AnnotationColor, kind: AnnotationKind) => {
    if (!selection || !annotationActions) return
    const current = selection
    setSelection(null)
    window.getSelection()?.removeAllRanges()
    try {
      const row = await annotationActions.create({
        block_id: '',
        source: 'en',
        start: 0,
        end: 0,
        quote: current.quote,
        color,
        kind,
        page: current.page,
        rects: current.rects,
      })
      // 荧光笔和批注建好后都打开编辑框：可写笔记，也可直接关掉保留标记
      if (row) {
        setEditing({ annotation: row, rect: current.rect })
      }
    } catch (err) {
      console.warn('PDF 标注失败：', err)
    }
  }

  const pages = Array.from({ length: total }, (_, index) => index + 1)

  return (
    <div className={`pdf-view ${useTextLayer ? 'pdf-view-text' : ''}`} ref={scrollRef}>
      {useTextLayer ? (
        <Document
          file={pdfUrl ?? undefined}
          loading={null}
          onLoadStart={() => setPdfReady(false)}
          onLoadSuccess={() => setPdfReady(true)}
          onLoadError={() => setPdfFailed(true)}
        >
          <div className={pagesClass}>
            {pages.map((page) => {
              const size = pageSizes?.[page - 1]
              // 拿不到页尺寸时退回 A4 比例，至少保证跳页时占位高度稳定
              const ratio = size && size[0] > 0 ? `${size[0]} / ${size[1]}` : '0.707'
              const aspect = size && size[0] > 0 ? size[0] / size[1] : 0.707
              return (
                <div
                  key={page}
                  className={`pdf-page-row${reviewMode ? ' is-review' : ''}${
                    reviewMode && stackedNotes ? ' is-stacked' : ''
                  }`}
                >
                  <figure
                    className="pdf-page"
                    data-page={page}
                    style={{ width: `${pageWidth}px` }}
                  >
                    <div
                      className="pdf-page-frame"
                      style={{ width: '100%', aspectRatio: ratio }}
                      onMouseUp={onFrameMouseUp}
                      onDoubleClick={onFrameDoubleClick}
                      onClick={onFrameClick}
                    >
                      {visible.has(page) && (
                        <Page
                          pageNumber={page}
                          width={pageWidth}
                          renderTextLayer
                          renderAnnotationLayer={false}
                          loading={<span className="pdf-page-loading" />}
                          error={<span className="pdf-page-loading">第 {page} 页加载失败</span>}
                        />
                      )}
                      <PdfAnnotationLayer page={page} annotations={annotations} />
                    </div>
                    <figcaption>第 {page} 页</figcaption>
                  </figure>
                  {reviewMode && (
                    <PdfMarginNotes
                      page={page}
                      annotations={annotations}
                      frameHeight={pageWidth / aspect}
                      stacked={stackedNotes}
                      actions={annotationActions}
                      focusAnnotation={focusAnnotation}
                    />
                  )}
                </div>
              )
            })}
          </div>
        </Document>
      ) : (
        <div className={pagesClass}>
          {pages.map((page) => {
            const size = pageSizes?.[page - 1]
            const ratio = size && size[0] > 0 ? `${size[0]} / ${size[1]}` : '0.707'
            const aspect = size && size[0] > 0 ? size[0] / size[1] : 0.707
            return (
              <div
                key={page}
                className={`pdf-page-row${reviewMode ? ' is-review' : ''}${
                  reviewMode && stackedNotes ? ' is-stacked' : ''
                }`}
              >
                <figure
                  className="pdf-page"
                  data-page={page}
                  style={{ width: `${pageWidth}px` }}
                >
                  <div
                    className="pdf-page-frame"
                    style={{ width: '100%', aspectRatio: ratio }}
                    onDoubleClick={onFrameDoubleClick}
                    onClick={onFrameClick}
                  >
                    <img
                      src={
                        pageUrls?.[page] ??
                        mediaUrl(
                          `/api/papers/${encodeURIComponent(paperId)}/page/${page}.png?width=${RENDER_WIDTH}`,
                        )
                      }
                      alt={`第 ${page} 页`}
                      loading={Math.abs(page - initialPage) <= 1 ? 'eager' : 'lazy'}
                      onLoad={() => {
                        if (page === initialPage && !jumped.current) {
                          jumped.current = true
                          scrollToPage(initialPage)
                        }
                      }}
                    />
                  </div>
                  <figcaption>第 {page} 页</figcaption>
                </figure>
                {reviewMode && (
                  <PdfMarginNotes
                    page={page}
                    annotations={annotations}
                    frameHeight={pageWidth / aspect}
                    stacked={stackedNotes}
                    actions={annotationActions}
                    focusAnnotation={focusAnnotation}
                  />
                )}
              </div>
            )
          })}
        </div>
      )}

      {selection && (
        <SelectionToolbar
          rect={selection.rect}
          text={selection.quote}
          onPick={(color) => void applyAnnotation(color, 'highlight')}
          onComment={(color) => void applyAnnotation(color, 'comment')}
          onAsk={
            onAskQuote
              ? () => {
                  const current = selection
                  setSelection(null)
                  window.getSelection()?.removeAllRanges()
                  onAskQuote({
                    text: current.quote,
                    page: current.page,
                    source: 'pdf',
                  })
                }
              : undefined
          }
          onClose={() => {
            setSelection(null)
            window.getSelection()?.removeAllRanges()
          }}
        />
      )}

      {editing && annotationActions && (
        <AnnotationPopover
          annotation={editing.annotation}
          rect={editing.rect}
          autoFocus={!editing.peek}
          onSave={async (patch) => {
            await annotationActions.update(editing.annotation.id, patch)
            setEditing(null)
          }}
          onDelete={async () => {
            await annotationActions.remove(editing.annotation.id)
            setEditing(null)
          }}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  )
}