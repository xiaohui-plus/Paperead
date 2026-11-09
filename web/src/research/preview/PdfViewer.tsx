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

/** PDF 预览：pdf.js 渲染，带翻页与缩放，文本可选中复制。 */
export function PdfViewer({ url, title }: Props) {
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [width, setWidth] = useState(720)
  const scrollRef = useRef<HTMLDivElement>(null)

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

  return (
    <div className="pdf-preview">
      <div className="pdf-preview-toolbar">
        <button
          type="button"
          disabled={page <= 1}
          onClick={() => setPage((prev) => Math.max(1, prev - 1))}
        >
          上一页
        </button>
        <span className="pdf-preview-page">
          <input
            type="number"
            min={1}
            max={pages || 1}
            value={page}
            onChange={(event) => {
              const next = Number(event.target.value)
              if (!Number.isFinite(next)) return
              setPage(Math.min(Math.max(1, next), pages || 1))
            }}
          />
          / {pages || '—'}
        </span>
        <button
          type="button"
          disabled={pages === 0 || page >= pages}
          onClick={() => setPage((prev) => Math.min(pages, prev + 1))}
        >
          下一页
        </button>
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

      <div className="pdf-preview-scroll" ref={scrollRef}>
        <Document
          file={url}
          loading={<p className="project-preview-note">正在加载 PDF…</p>}
          error={<p className="project-preview-note">PDF 加载失败，可右上角下载后查看。</p>}
          onLoadSuccess={({ numPages }) => {
            setPages(numPages)
            setPage((prev) => Math.min(prev, numPages) || 1)
          }}
        >
          <Page
            pageNumber={page}
            width={pageWidth}
            renderTextLayer
            renderAnnotationLayer
            loading={<p className="project-preview-note">正在渲染第 {page} 页…</p>}
          />
        </Document>
        <p className="pdf-preview-hint">{title}</p>
      </div>
    </div>
  )
}
