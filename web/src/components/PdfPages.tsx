import { useCallback, useEffect, useRef } from 'react'
import { mediaUrl } from '../lib/mobile'

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
export function PdfPages({
  paperId,
  pageCount,
  pageSizes,
  initialPage,
  width,
  jump,
  onPageChange,
  pageUrls,
}: Props) {
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

  // 左侧面板点目录 / 重点 / 图表：滚到对应页
  useEffect(() => {
    if (!jump) return
    scrollToPage(jump.page)
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

  return (
    <div className="pdf-view" ref={scrollRef}>
      <div className="pdf-pages">
        {Array.from({ length: total }, (_, index) => index + 1).map((page) => {
          const size = pageSizes?.[page - 1]
          // 拿不到页尺寸时退回 A4 比例，至少保证跳页时占位高度稳定
          const ratio = size && size[0] > 0 ? `${size[0]} / ${size[1]}` : '0.707'
          return (
            <figure key={page} className="pdf-page" data-page={page}>
              <div
                className="pdf-page-frame"
                style={{ width: `min(${width}px, 100%)`, aspectRatio: ratio }}
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
    </div>
  )
}
