import type { PaperRow } from '../lib/library'

interface Props {
  paper: PaperRow
  view: 'grid' | 'list'
  onOpen: (id: string) => void
  onDelete: (paper: PaperRow) => void
  onReparse: (paper: PaperRow) => void
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

export function PaperCard({ paper, view, onOpen, onDelete, onReparse }: Props) {
  const busy = paper.status === 'parsing'
  const hasTranslation = paper.translated > 0
  const failed = paper.status === 'failed'

  return (
    <article
      className={`paper-card ${view === 'list' ? 'is-list' : ''}`}
      onClick={() => onOpen(paper.id)}
      title={paper.title}
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
      </div>

      <div className="paper-body">
        <h3>{paper.title || paper.filename}</h3>
        <p className="paper-authors">{paper.authors || '作者未知'}</p>
        <div className="paper-meta">
          {paper.pages ? `${paper.pages} 页` : '—'}
          {paper.size ? ` · ${sizeLabel(paper.size)}` : ''}
          {hasTranslation ? ` · ${paper.translated} 段已译` : ''}
        </div>
      </div>

      <div className="paper-actions" onClick={(e) => e.stopPropagation()}>
        <button className="mini-btn" onClick={() => onOpen(paper.id)}>
          阅读
        </button>
        {failed && (
          <button className="mini-btn" onClick={() => onReparse(paper)}>
            重试
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
