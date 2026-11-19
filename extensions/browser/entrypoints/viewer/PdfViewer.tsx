import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as pdfjsLib from 'pdfjs-dist'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

interface Props {
  doc: PDFDocumentProxy
  bytes: Uint8Array | null
  filename: string
  fileUrl: string
  onPageChange?: (page: number) => void
}

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value))

function textOf(items: unknown[]): string {
  const parts: string[] = []
  for (const item of items) {
    if (item && typeof item === 'object' && 'str' in item) {
      const value = (item as { str?: unknown }).str
      if (typeof value === 'string') parts.push(value)
    }
  }
  return parts.join(' ')
}

function Page({
  doc,
  index,
  scale,
  fitWidth,
  rotation,
  width,
  query,
  hitPage,
  onMount,
}: {
  doc: PDFDocumentProxy
  index: number
  scale: number
  fitWidth: boolean
  rotation: number
  width: number
  query: string
  hitPage: boolean
  onMount: (index: number, element: HTMLDivElement | null) => void
}) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(index <= 2)
  const [rendered, setRendered] = useState(0)

  useEffect(() => {
    if (visible) return
    const element = wrapRef.current
    if (!element) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true)
          observer.disconnect()
        }
      },
      { rootMargin: '1200px' },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [visible])

  useEffect(() => {
    onMount(index, wrapRef.current)
    return () => onMount(index, null)
  }, [index, onMount])

  useEffect(() => {
    if (!visible) return
    const element = wrapRef.current
    if (!element) return
    let cancelled = false
    let textLayer: HTMLDivElement | null = null

    void (async () => {
      const page = await doc.getPage(index)
      if (cancelled) return
      const base = page.getViewport({ scale: 1, rotation })
      const effective = fitWidth
        ? clamp(Math.max(320, width - 48) / base.width, 0.35, 4)
        : scale
      const viewport = page.getViewport({ scale: effective, rotation })

      element.style.width = `${viewport.width}px`
      element.style.height = `${viewport.height}px`
      element.replaceChildren()
      element.dataset.scale = String(effective)

      const canvas = document.createElement('canvas')
      const ratio = window.devicePixelRatio || 1
      canvas.width = Math.floor(viewport.width * ratio)
      canvas.height = Math.floor(viewport.height * ratio)
      canvas.style.width = `${viewport.width}px`
      canvas.style.height = `${viewport.height}px`
      element.appendChild(canvas)

      const context = canvas.getContext('2d')
      if (!context) return
      await page.render({
        canvasContext: context,
        viewport,
        transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
      }).promise
      if (cancelled) return

      textLayer = document.createElement('div')
      textLayer.className = 'textLayer'
      textLayer.style.setProperty('--scale-factor', String(viewport.scale))
      element.appendChild(textLayer)
      const textContent = await page.getTextContent()
      if (cancelled) return
      const layer = new pdfjsLib.TextLayer({
        textContentSource: textContent,
        container: textLayer,
        viewport,
      })
      await layer.render()
      if (!cancelled) setRendered((value) => value + 1)
    })()

    return () => {
      cancelled = true
    }
  }, [doc, index, scale, fitWidth, rotation, width, visible])

  // 搜索高亮只动文字层，不重绘 canvas。浏览器原生查找对 canvas 无效，
  // 这里给命中的文字 span 加底色，配合工具栏的“第 n/N 处”跳页。
  useEffect(() => {
    const layer = wrapRef.current?.querySelector('.textLayer')
    if (!layer) return
    layer.querySelectorAll('span.pdf-hit').forEach((node) => {
      node.classList.remove('pdf-hit')
    })
    if (!query || !hitPage) return
    const needle = query.toLowerCase()
    layer.querySelectorAll('span').forEach((node) => {
      if ((node.textContent || '').toLowerCase().includes(needle)) {
        node.classList.add('pdf-hit')
      }
    })
  }, [query, hitPage, rendered, scale, fitWidth, rotation, width])

  return (
    <div
      className="page-wrap"
      id={`pdf-page-${index}`}
      ref={wrapRef}
      style={{ height: 1100, width: 'min(860px, calc(100vw - 48px))' }}
    />
  )
}

export default function PdfViewer({ doc, bytes, filename, fileUrl, onPageChange }: Props) {
  const [scale, setScale] = useState(1)
  const [fitWidth, setFitWidth] = useState(true)
  const [rotation, setRotation] = useState(0)
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState('1')
  const [width, setWidth] = useState(window.innerWidth)
  const [baseWidth, setBaseWidth] = useState(612)
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<number[]>([])
  const [hitIndex, setHitIndex] = useState(0)
  const [searching, setSearching] = useState(false)
  const [toast, setToast] = useState('')
  const pageRefs = useRef(new Map<number, HTMLDivElement>())
  const restoreTo = useRef<number | null>(null)
  const lastQuery = useRef('')

  useEffect(() => {
    let cancelled = false
    void doc.getPage(1).then((first) => {
      if (!cancelled) setBaseWidth(first.getViewport({ scale: 1 }).width)
    })
    return () => {
      cancelled = true
    }
  }, [doc])

  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const onMount = useCallback((index: number, element: HTMLDivElement | null) => {
    if (element) pageRefs.current.set(index, element)
    else pageRefs.current.delete(index)
  }, [])

  const scrollToPage = useCallback((target: number, smooth = true) => {
    const element = document.getElementById(`pdf-page-${target}`)
    if (!element) return
    element.scrollIntoView({ block: 'start', behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  useEffect(() => {
    let frame = 0
    const onScroll = () => {
      if (frame) return
      frame = window.requestAnimationFrame(() => {
        frame = 0
        let best = 1
        let bestDistance = Number.POSITIVE_INFINITY
        for (let index = 1; index <= doc.numPages; index += 1) {
          const element = pageRefs.current.get(index)
          if (!element) continue
          const distance = Math.abs(element.getBoundingClientRect().top - 56)
          if (distance < bestDistance) {
            bestDistance = distance
            best = index
          }
        }
        setPage(best)
      })
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => {
      window.removeEventListener('scroll', onScroll)
      if (frame) window.cancelAnimationFrame(frame)
    }
  }, [doc.numPages])

  useEffect(() => {
    setPageInput(String(page))
  }, [page])

  useEffect(() => {
    onPageChange?.(page)
  }, [page, onPageChange])

  useEffect(() => {
    if (restoreTo.current == null) return
    const target = restoreTo.current
    restoreTo.current = null
    const timer = window.setTimeout(() => scrollToPage(target, false), 80)
    return () => window.clearTimeout(timer)
  }, [scale, fitWidth, rotation, scrollToPage])

  const zoom = useCallback(
    (next: number) => {
      restoreTo.current = page
      setFitWidth(false)
      setScale(clamp(next, 0.35, 4))
    },
    [page],
  )

  useEffect(() => {
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return
      event.preventDefault()
      restoreTo.current = page
      setFitWidth(false)
      setScale((value) => clamp(value * (event.deltaY > 0 ? 0.9 : 1.1), 0.35, 4))
    }
    window.addEventListener('wheel', onWheel, { passive: false })
    return () => window.removeEventListener('wheel', onWheel)
  }, [page])

  const go = useCallback(
    (delta: number) => {
      const target = clamp(page + delta, 1, doc.numPages)
      setPage(target)
      scrollToPage(target)
    },
    [doc.numPages, page, scrollToPage],
  )

  const jumpToInput = useCallback(() => {
    const target = clamp(Number(pageInput) || 1, 1, doc.numPages)
    setPage(target)
    scrollToPage(target)
  }, [doc.numPages, pageInput, scrollToPage])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
      if (event.key === 'PageDown' || (event.key === ' ' && !event.shiftKey)) {
        event.preventDefault()
        go(1)
      } else if (event.key === 'PageUp' || (event.key === ' ' && event.shiftKey)) {
        event.preventDefault()
        go(-1)
      } else if (event.key === 'Home') {
        event.preventDefault()
        setPage(1)
        scrollToPage(1)
      } else if (event.key === 'End') {
        event.preventDefault()
        setPage(doc.numPages)
        scrollToPage(doc.numPages)
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault()
        setSearchOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [doc.numPages, go, scrollToPage])

  const runSearch = useCallback(
    async (value: string) => {
      const needle = value.trim()
      setQuery(needle)
      if (!needle) {
        setHits([])
        return
      }
      setSearching(true)
      const found: number[] = []
      try {
        for (let index = 1; index <= doc.numPages; index += 1) {
          const pageProxy = await doc.getPage(index)
          const content = await pageProxy.getTextContent()
          const text = textOf(content.items as unknown[])
          if (text.toLowerCase().includes(needle.toLowerCase())) found.push(index)
        }
        setHits(found)
        lastQuery.current = needle
        setHitIndex(0)
        const first = found[0]
        if (first != null) scrollToPage(first)
      } finally {
        setSearching(false)
      }
    },
    [doc, scrollToPage],
  )

  const nextHit = useCallback(() => {
    if (!hits.length) return
    const next = (hitIndex + 1) % hits.length
    const target = hits[next]
    setHitIndex(next)
    if (target != null) scrollToPage(target)
  }, [hitIndex, hits, scrollToPage])

  const download = useCallback(() => {
    if (!bytes) return
    const blob = new Blob([bytes as unknown as BlobPart], {
      type: 'application/pdf',
    })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 10000)
    setToast('已开始下载')
    window.setTimeout(() => setToast(''), 1800)
  }, [bytes, filename])

  const printable = useCallback(() => {
    setToast('打印使用浏览器原生查看器效果最好')
    window.setTimeout(() => setToast(''), 2200)
    window.print()
  }, [])

  const openNative = useCallback(() => {
    window.open(fileUrl, '_blank', 'noopener')
  }, [fileUrl])

  const currentScale = useMemo(() => {
    if (!fitWidth) return scale
    return clamp(Math.max(320, width - 48) / Math.max(baseWidth, 1), 0.35, 4)
  }, [baseWidth, fitWidth, scale, width])

  const totalHits = hits.length
  const hitLabel = searching
    ? '搜索中…'
    : query
      ? totalHits
        ? `${hitIndex + 1}/${totalHits} 页有结果`
        : '无结果'
      : ''

  return (
    <div className="pdf-viewer">
      <div className="pdf-toolbar">
        <div className="pdf-group">
          <button type="button" title="上一页 (PageUp)" onClick={() => go(-1)}>
            ‹
          </button>
          <input
            className="pdf-page-input"
            aria-label="页码"
            value={pageInput}
            onChange={(event) => setPageInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') jumpToInput()
            }}
            onBlur={jumpToInput}
          />
          <span className="pdf-muted">/ {doc.numPages}</span>
          <button type="button" title="下一页 (PageDown)" onClick={() => go(1)}>
            ›
          </button>
        </div>

        <div className="pdf-group">
          <button type="button" title="缩小" onClick={() => zoom(scale / 1.15)}>
            −
          </button>
          <span className="pdf-scale">{Math.round(currentScale * 100)}%</span>
          <button type="button" title="放大" onClick={() => zoom(scale * 1.15)}>
            +
          </button>
          <button
            type="button"
            className={fitWidth ? 'active' : ''}
            title="适应宽度"
            onClick={() => {
              restoreTo.current = page
              setFitWidth(true)
            }}
          >
            适应宽度
          </button>
          <button
            type="button"
            title="顺时针旋转"
            onClick={() => {
              restoreTo.current = page
              setRotation((value) => (value + 90) % 360)
            }}
          >
            旋转
          </button>
        </div>

        <div className="pdf-group">
          <button
            type="button"
            className={searchOpen ? 'active' : ''}
            title="搜索 (Ctrl+F)"
            onClick={() => setSearchOpen((value) => !value)}
          >
            搜索
          </button>
          <button type="button" title="下载 PDF 副本" onClick={download}>
            下载
          </button>
          <button type="button" title="打印" onClick={printable}>
            打印
          </button>
          <button type="button" title="浏览器原生阅读器（完整效果）" onClick={openNative}>
            浏览器打开
          </button>
        </div>
      </div>

      {searchOpen && (
        <div className="pdf-search">
          <input
            autoFocus
            value={query}
            placeholder="在当前 PDF 中搜索"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                const needle = query.trim()
                if (needle && needle !== lastQuery.current) {
                  void runSearch(needle)
                  return
                }
                if (event.shiftKey) {
                  if (hits.length) {
                    const prev = (hitIndex - 1 + hits.length) % hits.length
                    const target = hits[prev]
                    setHitIndex(prev)
                    if (target != null) scrollToPage(target)
                  }
                } else {
                  nextHit()
                }
              }
              if (event.key === 'Escape') setSearchOpen(false)
            }}
          />
          <span className="pdf-muted">{hitLabel}</span>
          <button type="button" disabled={!query.trim() || searching} onClick={() => void runSearch(query)}>
            查找
          </button>
          <button type="button" disabled={!hits.length} onClick={nextHit}>
            下一个
          </button>
          <button
            type="button"
            onClick={() => {
              setQuery('')
              setHits([])
              setSearchOpen(false)
            }}
          >
            关闭
          </button>
        </div>
      )}

      <div className="pages">
        {Array.from({ length: doc.numPages }, (_, index) => (
          <Page
            key={index}
            doc={doc}
            index={index + 1}
            scale={scale}
            fitWidth={fitWidth}
            rotation={rotation}
            width={width}
            query={query}
            hitPage={hits.includes(index + 1)}
            onMount={onMount}
          />
        ))}
      </div>

      {toast && <div className="pdf-toast">{toast}</div>}
    </div>
  )
}