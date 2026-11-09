import { useEffect, useRef, useState } from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

interface Props {
  url: string
  title: string
}

/** PDF 预览：所有页上下连续滚动、逐页分开，支持跳页与缩放，文本可选中复制。 */
export function PdfViewer({ url, title }: Props) {
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [width, setWidth] = useState(720)
  const scrollRef = useRef<HTMLDivElement>(null)
  const pageEls = useRef<(HTMLDivElement | null)[]>([])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width
      if (next) setWidth(next)
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  const pageWidth = Math.max(240, (width - 32) * zoom)

  /** 滚动时更新右下角/工具栏显示的当前页。 */
  const syncCurrentPage = () => {
    const container = scrollRef.current
    if (!container) return
    const top = container.getBoundingClientRect().top + 100
    let next = 1
    pageEls.current.forEach((el, index) => {
      if (el && el.getBoundingClientRect().top <= top) next = index + 1
    })
    setPage(next)
  }

  /** 跳页：把目标页滚到预览区顶部。 */
  const jumpTo = (target: number) => {
    const container = scrollRef.current
    const el = pageEls.current[target - 1]
    if (container && el) {
      const offset =
        el.getBoundingClientRect().top -
        container.getBoundingClientRect().top +
        container.scrollTop
      container.scrollTo({ top: offset, behavior: 'smooth' })
    }
    setPage(target)
  }

  return (
    <div className="pdf-preview">
      <div className="pdf-preview-toolbar">
        <span className="pdf-preview-page">
          第
          <input
            type="number"
            min={1}
            max={pages || 1}
            value={page}
            onChange={(event) => {
              const next = Number(event.target.value)
              if (!Number.isFinite(next) || next < 1) return
              jumpTo(Math.min(next, pages || 1))
            }}
          />
          页 / 共 {pages || '—'} 页
        </span>
        <i className="pdf-preview-sep" />
        <button
          type="button"
          disabled={zoom <= 0.5}
          title="缩小"
          onClick={() => setZoom((prev) => Math.max(0.5, prev - 0.25))}
        >
          −
        </button>
        <span className="pdf-preview-zoom">{Math.round(zoom * 100)}%</span>
        <button
          type="button"
          disabled={zoom >= 3}
          title="放大"
          onClick={() => setZoom((prev) => Math.min(3, prev + 0.25))}
        >
          +
        </button>
        <button type="button" onClick={() => setZoom(1)}>
          适应宽度
        </button>
      </div>

      <div className="pdf-preview-scroll" ref={scrollRef} onScroll={syncCurrentPage}>
        <Document
          file={url}
          loading={<p className="project-preview-note">正在加载 PDF…</p>}
          error={<p className="project-preview-note">PDF 加载失败，可右上角下载后查看。</p>}
          onLoadSuccess={({ numPages }) => {
            setPages(numPages)
            setPage(1)
          }}
        >
          {Array.from({ length: pages }, (_, index) => (
            <div
              key={index}
              className="pdf-preview-sheet"
              ref={(el) => {
                pageEls.current[index] = el
              }}
            >
              <Page
                pageNumber={index + 1}
                width={pageWidth}
                renderTextLayer
                renderAnnotationLayer
              />
            </div>
          ))}
        </Document>
        <p className="pdf-preview-hint">{title}</p>
      </div>
    </div>
  )
}
