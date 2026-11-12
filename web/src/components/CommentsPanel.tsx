import { useEffect, useMemo } from 'react'
import type { Paper } from '../types'
import type { Annotation, AnnotationColor } from '../lib/library'

interface Props {
  paper: Paper
  annotations: Annotation[]
  /** 跳到段落；PDF 标注传页码，切到原版 PDF 并滚到该页 */
  onJump: (blockId: string, page?: number) => void
  onUpdate: (id: string, patch: { color?: AnnotationColor; note?: string }) => Promise<void>
  onDelete: (id: string) => Promise<void>
  /** 双击正文标注后聚焦到这一条：滚动到卡片并短暂描边 */
  focusAnnotation?: { id: string; at: number } | null
}

const TYPE_LABEL: Record<Annotation['kind'], string> = {
  highlight: '荧光笔',
  comment: '批注',
}

/** 标注所属页码：PDF 标注自带页码，重排段落标注按 block 反查。 */
function pageOfAnnotation(item: Annotation, pageOf: Map<string, number>): number {
  return item.page ?? pageOf.get(item.block_id) ?? 0
}

/** 同一页内的纵向位置，用于批注栏排序（PDF 用选区 y，段落用字符偏移）。 */
function positionOf(item: Annotation): number {
  if (item.page) return (item.rects?.[0]?.y ?? 0) * 100000
  return item.start
}

/** 一条标注在原文、译文两侧的引用文本（只有被划的那侧一定存在）。 */
function sidesOf(item: Annotation): { en: string; zh: string } {
  const pair = item.pair
  if (item.source === 'zh') {
    return { zh: item.quote, en: pair?.source === 'en' ? pair.quote : '' }
  }
  return { en: item.quote, zh: pair?.source === 'zh' ? pair.quote : '' }
}

/** 把批注和荧光笔导出为 Markdown，方便粘进综述或 Zotero 笔记。 */
export function downloadNotes(paper: Paper, annotations: Annotation[]): void {
  const pageOf = new Map(paper.blocks.map((b) => [b.id, b.page]))
  const lines: string[] = [`# ${paper.title || '论文'} · 标注`, '']
  const ordered = [...annotations].sort(
    (a, b) =>
      pageOfAnnotation(a, pageOf) - pageOfAnnotation(b, pageOf) ||
      positionOf(a) - positionOf(b),
  )
  for (const item of ordered) {
    const page = pageOfAnnotation(item, pageOf) || '?'
    const sides = sidesOf(item)
    lines.push(`- P${page} · ${TYPE_LABEL[item.kind]} · ${item.color}`)
    if (sides.en) lines.push(`  > 原文：${sides.en.replace(/\n+/g, ' ')}`)
    if (sides.zh) lines.push(`  > 译文：${sides.zh.replace(/\n+/g, ' ')}`)
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

/**
 * 批注栏：正文里划词产生的荧光笔（填色）和下划线批注都收在这里，按页码排序。
 * 在原文上划的标注会在译文出现后自动补上对应片段，卡片同时展示原文与译文。
 */
export function CommentsPanel({
  paper,
  annotations,
  onJump,
  onUpdate,
  onDelete,
  focusAnnotation,
}: Props) {
  useEffect(() => {
    if (!focusAnnotation) return
    const el = document.getElementById(`note-${focusAnnotation.id}`)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    // 连续双击同一条也要重新闪：先摘掉动画类，强制回流后再挂上
    el.classList.remove('is-flash')
    void el.offsetWidth
    el.classList.add('is-flash')
    const timer = window.setTimeout(() => {
      el.classList.remove('is-flash')
    }, 1500)
    return () => {
      window.clearTimeout(timer)
      el.classList.remove('is-flash')
    }
  }, [focusAnnotation])

  const pageOf = useMemo(
    () => new Map(paper.blocks.map((b) => [b.id, b.page])),
    [paper.blocks],
  )
  const items = useMemo(
    () =>
      [...annotations].sort(
        (a, b) =>
          pageOfAnnotation(a, pageOf) - pageOfAnnotation(b, pageOf) ||
          positionOf(a) - positionOf(b),
      ),
    [annotations, pageOf],
  )
  const comments = items.filter((item) => item.kind === 'comment').length
  const highlights = items.length - comments

  if (!items.length) {
    return (
      <div className="tab-body">
        <div className="notes-empty">
          <p>在正文里选中一句话：点颜色是荧光笔填色，点「批注」是彩色下划线。</p>
          <p>原版 PDF 也能直接划词批注；原文上的标注会自动同步到译文对应句子，并都收进这里。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="tab-body notes-panel">
      <div className="notes-head">
        <span>
          {items.length} 条标注
          {comments > 0 && highlights > 0 ? ` · 批注 ${comments} · 荧光笔 ${highlights}` : ''}
        </span>
        <button className="link-btn" onClick={() => downloadNotes(paper, items)}>
          导出 Markdown
        </button>
      </div>
      <p className="notes-intro">
        原版 PDF 与重排正文都能划词；原文标注会同步标到译文同一句（PDF 页暂不映射译文）。
      </p>

      {items.map((item) => {
        const sides = sidesOf(item)
        const page = pageOfAnnotation(item, pageOf)
        const isPdf = Boolean(item.page)
        return (
          <article
            className="note-card"
            id={`note-${item.id}`}
            data-annotation-id={item.id}
            key={`${item.id}-${item.updated_at}`}
          >
            <div className="note-meta">
              <span
                className={`note-swatch note-${item.kind} note-${item.kind}-${item.color}`}
                aria-hidden
              />
              <span>P{page || '?'}</span>
              <span className="note-type">{TYPE_LABEL[item.kind]}</span>
              {isPdf && <span className="note-type note-type-quiet">PDF</span>}
              <button
                className="link-btn note-jump"
                onClick={() => onJump(item.block_id, item.page ?? undefined)}
              >
                跳到原文
              </button>
            </div>
            <div className="note-quotes">
              {sides.en && (
                <p className="note-quote">
                  <span className="note-side">原文</span>
                  {sides.en}
                </p>
              )}
              {sides.zh && (
                <p className="note-quote note-quote-zh">
                  <span className="note-side">译文</span>
                  {sides.zh}
                </p>
              )}
            </div>
            <textarea
              className="note-input"
              defaultValue={item.note}
              placeholder={item.kind === 'comment' ? '写下你的批注…' : '给这处荧光笔补一句笔记…'}
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
                  if (window.confirm(`删除这条${TYPE_LABEL[item.kind]}？`)) void onDelete(item.id)
                }}
              >
                删除
              </button>
            </div>
          </article>
        )
      })}
    </div>
  )
}