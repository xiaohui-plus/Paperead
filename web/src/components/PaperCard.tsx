import type { PaperRow } from '../lib/library'

interface Props {
  paper: PaperRow
  view: 'grid' | 'list'
  onOpen: (id: string) => void
  onDelete: (paper: PaperRow) => void
  onReparse: (paper: PaperRow) => void
  /** 下载到本机的进度；null 表示不在下载 */
  downloadProgress?: { done: number; total: number } | null
  onDownload?: (paper: PaperRow) => void
  draggable?: boolean
  dragging?: boolean
  onDragStart?: (paper: PaperRow) => void
  onDragEnd?: () => void
}

const STATUS_LABEL: Record<string, string> = {
  uploaded: '待解析',
  parsing: '解析中',
  ready: '已就绪',
  failed: '解析失败',
}

function sizeLabel(bytes: number): string {
  if (!bytes) return ''
  const mb = bytes / 1024 / 1024
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
}

export function PaperCard({
  paper,
  view,
  onOpen,
  onDelete,
  onReparse,
  downloadProgress = null,
  onDownload,
  draggable = false,
  dragging = false,
  onDragStart,
  onDragEnd,
}: Props) {
  const busy = paper.status === 'parsing'
  const hasTranslation = paper.translated > 0
  const failed = paper.status === 'failed'
  const readPct = Math.min(100, Math.max(0, paper.read_pct ?? 0))
  const finished = readPct >= 95

  return (
    <article
      className={`paper-card ${view === 'list' ? 'is-list' : ''}`}
      onClick={() => onOpen(paper.id)}
      title={paper.title}
      draggable={draggable}
      data-dragging={dragging}
      onDragStart={(event) => {
        if (!draggable) return
        event.dataTransfer.effectAllowed = 'move'
        event.dataTransfer.setData('text/plain', paper.id)
        onDragStart?.(paper)
      }}
      onDragEnd={() => onDragEnd?.()}
    >
      <div className="paper-cover">
        {paper.thumb_url ? (
          <img src={paper.thumb_url} alt="" loading="lazy" />
        ) : (
          <div className="cover-fallback">PDF</div>
        )}
        {paper.status !== 'ready' && (
          <span className="paper-status" data-status={paper.status}>
            {STATUS_LABEL[paper.status] ?? paper.status}
          </span>
        )}
        {paper.offline && (
          <span className="paper-local-badge">
            {paper.local_source === 'storyreader' ? '导入' : '已下载'}
          </span>
        )}
      </div>

      <div className="paper-body">
        <h3>{paper.title || paper.filename}</h3>
        <p className="paper-authors">{paper.authors || '作者未知'}</p>
        <div className="paper-meta">
          {paper.pages ? `${paper.pages} 页` : '—'}
          {paper.size ? ` · ${sizeLabel(paper.size)}` : ''}
          {hasTranslation ? ` · ${paper.translated} 段已译` : ''}
          {readPct > 0 && (
            <span className="paper-read-label">
              {' · '}
              {finished ? '已读完' : `读至 ${Math.round(readPct)}%`}
            </span>
          )}
          {paper.annotation_count ? ` · ${paper.annotation_count} 条标注` : ''}
        </div>
      </div>

      {readPct > 0 && (
        <div className="paper-read" aria-hidden>
          <i style={{ width: `${readPct}%` }} />
        </div>
      )}

      <div className="paper-actions" onClick={(e) => e.stopPropagation()}>
        <button className="mini-btn" onClick={() => onOpen(paper.id)}>
          {readPct > 0 && !finished ? '继续读' : '阅读'}
        </button>
        {failed && (
          <button className="mini-btn" onClick={() => onReparse(paper)}>
            重试
          </button>
        )}
        {!paper.offline && onDownload && paper.status === 'ready' && (
          <button
            className="mini-btn"
            onClick={() => onDownload(paper)}
            disabled={Boolean(downloadProgress)}
            title="把解析结果和各页图片存到本机，离线也能读"
          >
            {downloadProgress
              ? `下载 ${downloadProgress.total
                  ? Math.round((downloadProgress.done / downloadProgress.total) * 100)
                  : 0}%`
              : '下载'}
          </button>
        )}
        <button className="mini-btn danger" onClick={() => onDelete(paper)}>
          删除
        </button>
      </div>

      {paper.status === 'failed' && paper.error && (
        <div className="paper-error" title={paper.error}>
          {paper.error}
        </div>
      )}
      {busy && <div className="paper-progress" />}
    </article>
  )
}
