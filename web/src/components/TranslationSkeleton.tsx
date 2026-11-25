interface Props {
  pageCount: number
  pageSizes?: [number, number][]
  currentPage?: number
  phase: 'translating' | 'building' | 'idle'
  done: number
  total: number
  /** 外部引擎阶段文案（如「正在翻译段落 24/120」）；为空时回退到块数 */
  stageText?: string
}

const MAX_SHEETS = 3

const TITLE: Record<Props['phase'], string> = {
  translating: '正在翻译全文',
  building: '正在生成译文 PDF',
  idle: '译文 PDF 尚未生成',
}

/** 右栏译文还没就绪时的纸张骨架：先给出与原文同比例的页面，中间提示进度。 */
export function TranslationSkeleton({
  pageCount,
  pageSizes,
  currentPage = 1,
  phase,
  done,
  total,
  stageText = '',
}: Props) {
  const count = Math.max(1, pageCount)
  const current = Math.max(1, Math.min(currentPage, count))
  const sheets = Math.min(MAX_SHEETS, count)
  const start = Math.max(1, Math.min(current - 1, count - sheets + 1))
  const pages = Array.from({ length: sheets }, (_, index) => start + index)
  const busy = phase !== 'idle'
  const ratio = (page: number) => {
    const size = pageSizes?.[page - 1]
    return size && size[0] > 0 ? size[0] / size[1] : 0.707
  }
  const meta =
    phase === 'translating'
      ? stageText ||
        (total > 0 ? `${done} / ${total} 段` : '正在准备翻译任务…')
      : phase === 'building'
        ? '正在排版每一页，完成后自动显示'
        : done > 0
          ? '译文已就绪，点顶部「全文翻译」重新生成'
          : '配好模型后，点顶部「全文翻译」即可开始'

  return (
    <div className="dual-skeleton" role="status" aria-live="polite">
      <div className="dual-skeleton-pages" aria-hidden>
        {pages.map((page) => (
          <div
            key={page}
            className="dual-skeleton-sheet"
            style={{ aspectRatio: ratio(page) }}
          />
        ))}
      </div>
      <div className="dual-skeleton-status">
        <div className="dual-skeleton-card">
          {busy && <i className="dual-spinner" />}
          <b className="dual-skeleton-title">{TITLE[phase]}</b>
          <span className="dual-skeleton-meta">{meta}</span>
        </div>
      </div>
    </div>
  )
}