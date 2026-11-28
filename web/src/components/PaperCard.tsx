import type { PaperRow } from '../lib/library'
import { IconDownload, IconRefresh, IconTrash } from './icons'

interface Props {
  paper: PaperRow
  view: 'grid' | 'list'
  onOpen: (id: string) => void
  onDelete: (paper: PaperRow) => void
  onReparse: (paper: PaperRow) => void
  /** 把论文 PDF 下载到本机 */
  onDownloadPdf?: (paper: PaperRow) => void
  downloading?: boolean
  draggable?: boolean
  dragging?: boolean
  /** 拖动排序时，这张卡片要显示「插到它前面 / 后面」的落点线 */
  insertPosition?: 'before' | 'after' | null
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

/**
 * 拖拽时跟随鼠标的小卡片。用 DOM 手工搭而不是 innerHTML，避免标题里的特殊字符被当成标签。
 * 元素必须短暂挂在文档里，浏览器才能把它截成 drag image；调用方在下一帧移除。
 */
function buildDragGhost(paper: PaperRow): HTMLElement {
  const ghost = document.createElement('div')
  ghost.className = 'paper-drag-ghost'

  const cover = document.createElement('div')
  cover.className = 'paper-drag-ghost-cover'
  // 缩略图可能尚未解码，先垫一层「PDF」字样，图片加载完成后自然盖住
  const fallback = document.createElement('span')
  fallback.textContent = 'PDF'
  cover.appendChild(fallback)
  if (paper.thumb_url) {
    const img = document.createElement('img')
    img.src = paper.thumb_url
    img.alt = ''
    cover.appendChild(img)
  }

  const body = document.createElement('div')
  body.className = 'paper-drag-ghost-body'
  const title = document.createElement('b')
  title.textContent = paper.title || paper.filename || '未命名'
  const meta = document.createElement('span')
  meta.textContent =
    [paper.authors, paper.pages ? `${paper.pages} 页` : '']
      .filter(Boolean)
      .join(' · ') || '文献'
  body.append(title, meta)

  const handle = document.createElement('i')
  handle.className = 'paper-drag-ghost-handle'
  handle.setAttribute('aria-hidden', 'true')

  ghost.append(cover, body, handle)
  document.body.appendChild(ghost)
  return ghost
}

export function PaperCard({
  paper,
  view,
  onOpen,
  onDelete,
  onReparse,
  onDownloadPdf,
  downloading = false,
  draggable = false,
  dragging = false,
  insertPosition = null,
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
      data-paper-id={paper.id}
      data-insert={insertPosition ?? undefined}
      onClick={() => onOpen(paper.id)}
      title={paper.title}
      draggable={draggable}
      data-dragging={dragging}
      onDragStart={(event) => {
        if (!draggable) return
        event.dataTransfer.effectAllowed = 'move'
        event.dataTransfer.setData('text/plain', paper.id)
        // 换成紧凑的跟随卡片，让用户在拖动时仍能看清是哪篇文献
        const ghost = buildDragGhost(paper)
        try {
          event.dataTransfer.setDragImage(ghost, 26, 24)
        } catch {
          // 个别环境不支持自定义 drag image，退回浏览器默认截图
        }
        window.setTimeout(() => ghost.remove(), 0)
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
        {!paper.offline && !busy && (
          <button
            type="button"
            className="paper-icon-btn"
            onClick={() => onReparse(paper)}
            title={failed ? '重新解析（上次失败）' : '重新解析'}
            aria-label="重新解析"
          >
            <IconRefresh />
          </button>
        )}
        {!paper.offline && onDownloadPdf && (
          <button
            type="button"
            className="paper-icon-btn"
            onClick={() => onDownloadPdf(paper)}
            disabled={downloading}
            title={downloading ? '正在下载 PDF…' : '下载 PDF 到本地'}
            aria-label="下载 PDF 到本地"
          >
            <span className={downloading ? 'paper-icon-spin' : undefined}>
              <IconDownload />
            </span>
          </button>
        )}
        <button
          type="button"
          className="paper-icon-btn danger"
          onClick={() => onDelete(paper)}
          title="删除论文"
          aria-label="删除论文"
        >
          <IconTrash />
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