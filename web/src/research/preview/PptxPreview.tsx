import { useEffect, useRef, useState } from 'react'

interface Props {
  url: string
  name: string
}

interface Viewer {
  preview(file: ArrayBuffer): Promise<unknown>
  destroy(): void
}

const MIN_WIDTH = 320
const MAX_WIDTH = 1080
/** 两侧留白，避免被预览区 padding 挤到横向滚动。 */
const HOST_PADDING = 18

/**
 * PPTX 预览：用 pptx-preview 在前端解析并渲染幻灯片，
 * 按容器宽度等比缩放，所有页纵向排列，外层负责滚动。
 * 库体积较大（含 echarts），只在这里按需加载。
 */
export function PptxPreview({ url, name }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const viewerRef = useRef<Viewer | null>(null)
  const [buffer, setBuffer] = useState<ArrayBuffer | null>(null)
  const [width, setWidth] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError('')
    setBuffer(null)
    fetch(url)
      .then((response) => {
        if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`)
        return response.arrayBuffer()
      })
      .then((data) => {
        if (alive) setBuffer(data)
      })
      .catch((loadError: unknown) => {
        if (!alive) return
        setError(loadError instanceof Error ? loadError.message : '读取失败')
        setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [url])

  // 容器宽度变化时重新渲染，保证幻灯片始终贴合预览区。
  useEffect(() => {
    const element = host.current
    if (!element) return
    const update = () => {
      const next = Math.max(
        MIN_WIDTH,
        Math.min(MAX_WIDTH, Math.round(element.clientWidth - HOST_PADDING * 2)),
      )
      setWidth((prev) => (Math.abs(prev - next) < 24 ? prev : next))
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const element = host.current
    if (!buffer || !element || width <= 0) return
    let alive = true
    setLoading(true)
    setError('')

    const render = async () => {
      const { init } = await import('pptx-preview')
      if (!alive) return
      viewerRef.current?.destroy()
      element.innerHTML = ''
      const viewer = init(element, { width, mode: 'list' })
      viewerRef.current = viewer
      try {
        await viewer.preview(buffer.slice(0))
        if (alive) setLoading(false)
      } catch (renderError) {
        if (!alive) return
        setError(
          renderError instanceof Error
            ? `幻灯片解析失败：${renderError.message}`
            : '幻灯片解析失败',
        )
        setLoading(false)
      }
    }
    void render()

    return () => {
      alive = false
      viewerRef.current?.destroy()
      viewerRef.current = null
      element.innerHTML = ''
    }
  }, [buffer, width])

  return (
    <div className="ppt-preview">
      <div ref={host} className="ppt-preview-host" aria-label={`${name} 幻灯片预览`} />
      {error ? (
        <p className="project-preview-note">{error}</p>
      ) : loading ? (
        <p className="project-preview-note">正在解析幻灯片…</p>
      ) : null}
    </div>
  )
}
