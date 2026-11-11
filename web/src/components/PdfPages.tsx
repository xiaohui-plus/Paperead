import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'react-pdf/dist/Page/TextLayer.css'
import { mediaUrl } from '../lib/mobile'
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
  /** 面板里点目录/重点/图表时滚到对应页；token 变化即触发 */
  jump: { page: number; token: number } | null
  onPageChange: (page: number) => void
  /** 离线页图（页码 → blob URL）；有值时不再请求服务器 */
  pageUrls?: Record<number, string>
  /** PDF 文件地址；有值时用 pdf.js 文本层渲染，可在原文上划词批注 */
  pdfUrl?: string | null
  annotations?: Annotation[]
  annotationActions?: AnnotationActions
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
  return (
    <div className="pdf-annot-layer" aria-hidden>
      {items.flatMap((item) =>
        (item.rects ?? []).map((rect, index) => (
          <span
            key={`${item.id}-${index}`}
            className={`pdf-annot pdf-annot-${item.kind} pdf-annot-${item.color}`}
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

// 服务端按这个宽度渲染，前端只做等比缩放；再大对公式也没有额外增益。
const RENDER_WIDTH = 1200

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
  jump,
  onPageChange,
  pageUrls,
  pdfUrl,
  annotations = [],
  annotationActions,
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
  const [editing, setEditing] = useState<{ annotation: Annotation; rect: DOMRect } | null>(null)
  const useTextLayer = Boolean(pdfUrl) && !pdfFailed

  const pageWidth = Math.max(320, Math.min(width, available || width))

  const scrollToPage = useCallback((page: number) => {
    scrollRef.current
      ?.querySelector<HTMLElement>(`[data-page="${page}"]`)
      ?.scrollIntoView({ block: 'start' })
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

  // 测量可用宽度：页宽不能超过阅读区，否则文本层会被裁掉
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const measure = () => setAvailable(Math.max(320, el.clientWidth - 56))
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

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
    if (!annotationActions) return
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
      const rects = [...range.getClientRects()]
        .filter((rect) => rect.width > 1 && rect.height > 1)
        .map((rect) => normalizeRect(rect, frameRect))
        .filter((rect) => rect.w > 0 && rect.h > 0)
      if (!rects.length) return
      setSelection({ page, quote, rects, rect: range.getBoundingClientRect() })
    }, 0)
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
          <div className="pdf-pages">
            {pages.map((page) => {
              const size = pageSizes?.[page - 1]
              // 拿不到页尺寸时退回 A4 比例，至少保证跳页时占位高度稳定
              const ratio = size && size[0] > 0 ? `${size[0]} / ${size[1]}` : '0.707'
              return (
                <figure
                  key={page}
                  className="pdf-page"
                  data-page={page}
                  style={{ width: `min(${pageWidth}px, 100%)` }}
                >
                  <div
                    className="pdf-page-frame"
                    style={{ width: '100%', aspectRatio: ratio }}
                    onMouseUp={onFrameMouseUp}
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
              )
            })}
          </div>
        </Document>
      ) : (
        <div className="pdf-pages">
          {pages.map((page) => {
            const size = pageSizes?.[page - 1]
            const ratio = size && size[0] > 0 ? `${size[0]} / ${size[1]}` : '0.707'
            return (
              <figure
                key={page}
                className="pdf-page"
                data-page={page}
                style={{ width: `min(${width}px, 100%)` }}
              >
                <div
                  className="pdf-page-frame"
                  style={{ width: '100%', aspectRatio: ratio }}
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
            )
          })}
        </div>
      )}

      {selection && (
        <SelectionToolbar
          rect={selection.rect}
          onPick={(color) => void applyAnnotation(color, 'highlight')}
          onComment={() => void applyAnnotation('blue', 'comment')}
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