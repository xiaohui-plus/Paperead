import { useMemo } from 'react'
import type { Paper } from '../types'
import type { Annotation, AnnotationColor } from '../lib/library'

interface Props {
  paper: Paper
  annotations: Annotation[]
  onJump: (blockId: string) => void
  onUpdate: (id: string, patch: { color?: AnnotationColor; note?: string }) => Promise<void>
  onDelete: (id: string) => Promise<void>
}

const SOURCE_LABEL: Record<'en' | 'zh', string> = {
  en: '原文',
  zh: '译文',
}

/** 把高亮和笔记导出为 Markdown，方便粘进综述或 Zotero 笔记。 */
export function downloadNotes(paper: Paper, annotations: Annotation[]): void {
  const pageOf = new Map(paper.blocks.map((b) => [b.id, b.page]))
  const lines: string[] = [`# ${paper.title || '论文'} · 笔记`, '']
  const ordered = [...annotations].sort(
    (a, b) =>
      (pageOf.get(a.block_id) ?? 0) - (pageOf.get(b.block_id) ?? 0) ||
      a.start - b.start,
  )
  for (const item of ordered) {
    lines.push(`- P${pageOf.get(item.block_id) ?? '?'} · ${SOURCE_LABEL[item.source]}`)
    lines.push(`  > ${item.quote.replace(/\n+/g, ' ')}`)
    if (item.note.trim()) lines.push(`  ${item.note.trim().replace(/\n+/g, '\n  ')}`)
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${(paper.title || 'paper').slice(0, 40)}-notes.md`
  a.click()
  URL.revokeObjectURL(url)
}

export function NotesPanel({ paper, annotations, onJump, onUpdate, onDelete }: Props) {
  const pageOf = useMemo(
    () => new Map(paper.blocks.map((b) => [b.id, b.page])),
    [paper.blocks],
  )
  const ordered = useMemo(
    () =>
      [...annotations].sort(
        (a, b) =>
          (pageOf.get(a.block_id) ?? 0) - (pageOf.get(b.block_id) ?? 0) ||
          a.start - b.start,
      ),
    [annotations, pageOf],
  )

  if (!annotations.length) {
    return (
      <div className="tab-body">
        <div className="notes-empty">
          <p>在正文里划词即可高亮。</p>
          <p>点已有高亮可以改颜色、写笔记；所有笔记只存在本机，也能一键导出 Markdown。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="tab-body notes-panel">
      <div className="notes-head">
        <span>
          {annotations.length} 条标注 · {annotations.filter((a) => a.note.trim()).length} 条笔记
        </span>
        <button className="link-btn" onClick={() => downloadNotes(paper, annotations)}>
          导出 Markdown
        </button>
      </div>

      {ordered.map((item) => (
        <article className="note-card" key={`${item.id}-${item.updated_at}`}>
          <div className="note-meta">
            <span className={`hl-dot hl-${item.color}`} aria-hidden />
            <span>P{pageOf.get(item.block_id) ?? '?'}</span>
            <span>{SOURCE_LABEL[item.source]}</span>
            <button className="link-btn note-jump" onClick={() => onJump(item.block_id)}>
              跳到原文
            </button>
          </div>
          <p className="note-quote">“{item.quote}”</p>
          <textarea
            className="note-input"
            defaultValue={item.note}
            placeholder="写点笔记…"
            rows={2}
            onBlur={(event) => {
              const next = event.target.value
              if (next !== item.note) void onUpdate(item.id, { note: next })
            }}
          />
          <div className="note-actions">
            <button
              className="link-btn danger"
              onClick={() => {
                if (window.confirm('删除这条标注？')) void onDelete(item.id)
              }}
            >
              删除
            </button>
          </div>
        </article>
      ))}
    </div>
  )
}
