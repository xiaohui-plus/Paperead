import { useCallback, useEffect, useRef } from 'react'

interface Props {
  paperId: string
  pageCount: number
  /** 切到原版时停留的页码（1 起），进来后直接滚到这一页 */
  initialPage: number
  /** 页面显示宽度（px）；图片按固定分辨率取，缩放只改 CSS 宽度 */
  width: number
  onPageChange: (page: number) => void
}

// 服务端按这个宽度渲染，前端只做等比缩放；再大对公式也没有额外增益。
const RENDER_WIDTH = 1200

/**
 * 原版 PDF 视图：逐页图片竖排、连续滚动。
 *
 * 不用 `<iframe src="*.pdf">`：桌面版跑在 WebView2 里，内置 PDF 阅读器是否可用
 * 取决于运行时与策略，空白页时用户无从判断；服务端用 PyMuPDF 出图则任何环境都能看。
 * 需要原生阅读器（搜索/打印）时，顶栏还有「新窗口 ↗」直链。
 */
export function PdfPages({ paperId, pageCount, initialPage, width, onPageChange }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const jumped = useRef(false)
  const total = Math.max(1, pageCount)

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

  return (
    <div className="pdf-view" ref={scrollRef}>
      <div className="pdf-pages">
        {Array.from({ length: total }, (_, index) => index + 1).map((page) => (
          <figure key={page} className="pdf-page" data-page={page}>
            <img
              src={`/api/papers/${encodeURIComponent(paperId)}/page/${page}.png?width=${RENDER_WIDTH}`}
              style={{ width: `${width}px` }}
              alt={`第 ${page} 页`}
              loading={Math.abs(page - initialPage) <= 1 ? 'eager' : 'lazy'}
              onLoad={() => {
                if (page === initialPage && !jumped.current) {
                  jumped.current = true
                  scrollToPage(initialPage)
                }
              }}
            />
            <figcaption>第 {page} 页</figcaption>
          </figure>
        ))}
      </div>
    </div>
  )
}
