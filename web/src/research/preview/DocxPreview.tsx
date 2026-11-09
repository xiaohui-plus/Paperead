import { useEffect, useRef, useState } from 'react'

interface Props {
  url: string
  name: string
}

/** A4 纸张在 96 DPI 下的像素尺寸，以及 Word 默认的 2.54cm 页边距。 */
const A4_PAGE = {
  width: '794px',
  minHeight: '1123px',
  padding: '96px',
} as const

/** 页面缩放下限，避免窗口很窄时文字小到看不清。 */
const MIN_PAGE_ZOOM = 0.5

/**
 * 有些 docx（例如 pandoc 导出的）只有空的 <w:sectPr/>，缺少纸张尺寸和页边距，
 * docx-preview 会退化成按内容收缩的小卡片。这里只给缺尺寸的页补 A4 默认值。
 */
function applyFallbackPageStyle(host: HTMLElement) {
  host.querySelectorAll<HTMLElement>('section.docx').forEach((page) => {
    if (page.style.width) return
    page.style.width = A4_PAGE.width
    page.style.minHeight = A4_PAGE.minHeight
    page.style.padding = A4_PAGE.padding
  })
}

/** 纸张比预览区宽时等比缩小，避免出现横向滚动条；窗口变宽后恢复原尺寸。 */
function fitPageZoom(host: HTMLElement) {
  const pages = Array.from(host.querySelectorAll<HTMLElement>('section.docx'))
  const available = host.clientWidth
  if (!pages.length || available <= 0) return
  let zoom = 1
  for (const page of pages) {
    const width = Number.parseFloat(page.style.width)
    if (width > available) zoom = Math.min(zoom, available / width)
  }
  zoom = Math.max(MIN_PAGE_ZOOM, zoom)
  pages.forEach((page) => {
    page.style.zoom = String(zoom)
  })
}

/**
 * Word 预览：docx-preview 在前端解析 .docx，按分页渲染成白底纸张，
 * 外层滚动、页与页之间保留间距，尽量贴近 Word 里的版式。
 * 解析库体积较大，只在打开 docx 时按需加载。
 */
export function DocxPreview({ url, name }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError('')

    const render = async () => {
      const response = await fetch(url)
      if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`)
      const buffer = await response.arrayBuffer()
      const { renderAsync } = await import('docx-preview')
      if (!alive) return
      const element = host.current
      if (!element) return
      element.innerHTML = ''
      await renderAsync(buffer, element, undefined, {
        className: 'docx',
        inWrapper: true,
        breakPages: true,
        ignoreWidth: false,
        ignoreHeight: false,
        renderHeaders: true,
        renderFooters: true,
        renderFootnotes: true,
        useBase64URL: true,
      })
      applyFallbackPageStyle(element)
      fitPageZoom(element)
      if (alive) setLoading(false)
    }

    render().catch((renderError: unknown) => {
      if (!alive) return
      setError(
        renderError instanceof Error
          ? `Word 解析失败：${renderError.message}`
          : 'Word 解析失败',
      )
      setLoading(false)
    })

    return () => {
      alive = false
      if (host.current) host.current.innerHTML = ''
    }
  }, [url])

  useEffect(() => {
    const element = host.current
    if (!element) return
    const observer = new ResizeObserver(() => fitPageZoom(element))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return (
    <div className="docx-preview">
      <div ref={host} className="docx-preview-host" aria-label={`${name} 文档预览`} />
      {error ? (
        <p className="project-preview-note">{error}</p>
      ) : loading ? (
        <p className="project-preview-note">正在解析 Word 文档…</p>
      ) : null}
    </div>
  )
}
