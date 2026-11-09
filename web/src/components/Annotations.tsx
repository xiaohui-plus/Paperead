import { useEffect, useRef, useState, type ReactNode } from 'react'
import type {
  Annotation,
  AnnotationColor,
  HighlightCategory,
  ReadingHighlight,
} from '../lib/library'

/** 自动重点的中文名，正文 tooltip 和「阅读重点」面板共用。 */
export const HIGHLIGHT_LABELS: Record<HighlightCategory, string> = {
  objective: '研究目标',
  method: '研究方法',
  conclusion: '主要结论',
}

/** 数字引用 [12] / [3-5]，或作者-年份 (Zhang et al. 2018) */
export const CITE_RE =
  /\[\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*\]|\([A-Z][A-Za-z\-']+(?:\s+(?:et\s+al\.?|and\s+[A-Z][A-Za-z\-']+))?\s*,?\s*(?:19|20)\d{2}[a-z]?\)/g

export const HL_COLORS: { key: AnnotationColor; label: string }[] = [
  { key: 'yellow', label: '黄' },
  { key: 'green', label: '绿' },
  { key: 'blue', label: '蓝' },
  { key: 'pink', label: '粉' },
]

/** 阅读页传给段落的标注读写接口，统一由 ReaderView 持有状态。 */
export interface AnnotationActions {
  create: (input: {
    block_id: string
    source: 'en' | 'zh'
    start: number
    end: number
    quote: string
    color: AnnotationColor
  }) => Promise<void>
  update: (
    id: string,
    patch: { color?: AnnotationColor; note?: string },
  ) => Promise<void>
  remove: (id: string) => Promise<void>
}

export function hasCitation(text: string): boolean {
  CITE_RE.lastIndex = 0
  return CITE_RE.test(text)
}

interface Span {
  start: number
  end: number
  kind: 'cite' | 'hl' | 'auto' | 'term'
  marker?: string
  annotation?: Annotation
  term?: string
  highlight?: ReadingHighlight
}

/**
 * 把原文/译文切成互不重叠的片段：引用编号优先，其次用户高亮、自动重点，最后术语。
 * 重叠时保留优先级高的那个，避免嵌套 <mark> 造成布局错乱。
 */
function buildSpans(
  text: string,
  source: 'en' | 'zh',
  annotations: Annotation[],
  highlights: ReadingHighlight[],
  terms: Record<string, string>,
): Span[] {
  const spans: Span[] = []

  const citeRe = new RegExp(CITE_RE.source, 'g')
  for (const m of text.matchAll(citeRe)) {
    if (m.index == null) continue
    spans.push({
      start: m.index,
      end: m.index + m[0].length,
      kind: 'cite',
      marker: m[0],
    })
  }

  for (const annotation of annotations) {
    let start = annotation.start
    let end = annotation.end
    // 重新解析或重译后偏移可能失配：按原文片段重新定位，找不到就不画
    if (text.slice(start, end) !== annotation.quote) {
      const found = annotation.quote ? text.indexOf(annotation.quote) : -1
      if (found < 0) continue
      start = found
      end = found + annotation.quote.length
    }
    if (end > start) spans.push({ start, end, kind: 'hl', annotation })
  }

  for (const highlight of highlights) {
    const quote = source === 'en' ? highlight.quote_en : highlight.quote_zh
    let start = source === 'en' ? highlight.start : highlight.zh_start
    let end = source === 'en' ? highlight.end : highlight.zh_end
    if (text.slice(start, end) !== quote) {
      const found = quote ? text.indexOf(quote) : -1
      if (found < 0) continue
      start = found
      end = found + quote.length
    }
    if (end > start) spans.push({ start, end, kind: 'auto', highlight })
  }

  const entries = Object.entries(terms ?? {})
    .filter(([zh]) => zh.length >= 2)
    .sort((a, b) => b[0].length - a[0].length)
  for (const [zh, en] of entries) {
    let from = 0
    for (;;) {
      const at = text.indexOf(zh, from)
      if (at < 0) break
      spans.push({ start: at, end: at + zh.length, kind: 'term', term: en })
      from = at + zh.length
    }
  }

  const priority = { cite: 0, hl: 1, auto: 2, term: 3 } as const
  spans.sort((a, b) => a.start - b.start || priority[a.kind] - priority[b.kind])
  const kept: Span[] = []
  let cursor = 0
  for (const span of spans) {
    if (span.start < cursor) continue
    kept.push(span)
    cursor = span.end
  }
  return kept
}

interface RichProps {
  text: string
  source: 'en' | 'zh'
  annotations: Annotation[]
  highlights?: ReadingHighlight[]
  terms?: Record<string, string>
  className: string
  onCite?: (marker: string, rect: DOMRect, el: HTMLElement) => void
  onAnnotationClick?: (annotation: Annotation, rect: DOMRect) => void
}

/** 正文段落：把引用编号、用户高亮、自动重点、术语标进同一段文本。 */
export function RichText({
  text,
  source,
  annotations,
  highlights = [],
  terms = {},
  className,
  onCite,
  onAnnotationClick,
}: RichProps) {
  const spans = buildSpans(
    text,
    source,
    annotations.filter((a) => a.source === source),
    highlights,
    terms,
  )
  const nodes: ReactNode[] = []
  let cursor = 0
  // 悬停稍等一下再开预览，划词或快速划过整段时不会频繁弹层
  const hoverTimer = useRef<number | undefined>(undefined)
  const cancelHover = () => {
    if (hoverTimer.current) {
      window.clearTimeout(hoverTimer.current)
      hoverTimer.current = undefined
    }
  }
  useEffect(() => cancelHover, [])

  for (const span of spans) {
    if (span.start > cursor) {
      nodes.push(<span key={`t${cursor}`}>{text.slice(cursor, span.start)}</span>)
    }
    if (span.kind === 'cite') {
      const marker = span.marker ?? ''
      nodes.push(
        <button
          key={`c${span.start}`}
          type="button"
          className="cite-mark"
          title="查看这条引用"
          onMouseDown={(e) => e.preventDefault()}
          onMouseEnter={(e) => {
            if (!onCite) return
            const el = e.currentTarget
            cancelHover()
            hoverTimer.current = window.setTimeout(() => {
              hoverTimer.current = undefined
              onCite(marker, el.getBoundingClientRect(), el)
            }, 260)
          }}
          onMouseLeave={cancelHover}
          onClick={(e) => {
            e.stopPropagation()
            cancelHover()
            onCite?.(marker, e.currentTarget.getBoundingClientRect(), e.currentTarget)
          }}
        >
          {text.slice(span.start, span.end)}
        </button>,
      )
    } else if (span.kind === 'hl' && span.annotation) {
      const annotation = span.annotation
      nodes.push(
        <mark
          key={`h${span.start}`}
          className={`hl hl-${annotation.color}`}
          data-aid={annotation.id}
          title={annotation.note || '点击编辑这条标注'}
          onClick={(e) => {
            e.stopPropagation()
            onAnnotationClick?.(annotation, (e.target as HTMLElement).getBoundingClientRect())
          }}
        >
          {text.slice(span.start, span.end)}
        </mark>,
      )
    } else if (span.kind === 'auto' && span.highlight) {
      const category = span.highlight.category
      nodes.push(
        <mark
          key={`a${span.start}`}
          className={`auto-hl auto-${category}`}
          title={`自动标记：${HIGHLIGHT_LABELS[category]}`}
        >
          {text.slice(span.start, span.end)}
        </mark>,
      )
    } else {
      nodes.push(
        <mark key={`m${span.start}`} className="term-hl" title={span.term}>
          {text.slice(span.start, span.end)}
        </mark>,
      )
    }
    cursor = span.end
  }
  if (cursor < text.length) nodes.push(<span key={`t${cursor}`}>{text.slice(cursor)}</span>)

  return (
    <p className={className} data-text-source={source}>
      {nodes}
    </p>
  )
}

/** 划词后浮在选区上方的颜色条。 */
export function SelectionToolbar({
  rect,
  onPick,
  onClose,
}: {
  rect: DOMRect
  onPick: (color: AnnotationColor) => void
  onClose: () => void
}) {
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (!(event.target as HTMLElement).closest('.hl-toolbar')) onClose()
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [onClose])

  const width = 188
  const left = Math.min(
    window.innerWidth - width - 12,
    Math.max(12, rect.left + rect.width / 2 - width / 2),
  )
  const top = rect.top > 64 ? rect.top - 48 : rect.bottom + 10

  return (
    <div
      className="hl-toolbar"
      style={{ left, top }}
      onMouseDown={(e) => e.preventDefault()}
    >
      <span className="hl-toolbar-label">标记</span>
      {HL_COLORS.map((c) => (
        <button
          key={c.key}
          type="button"
          className={`hl-dot hl-${c.key}`}
          title={c.label}
          onClick={() => onPick(c.key)}
        />
      ))}
      <button type="button" className="hl-toolbar-close" onClick={onClose} title="取消">
        ×
      </button>
    </div>
  )
}

/** 点击已有高亮：改颜色、写笔记或删除。 */
export function AnnotationPopover({
  annotation,
  rect,
  onSave,
  onDelete,
  onClose,
}: {
  annotation: Annotation
  rect: DOMRect
  onSave: (patch: { color: AnnotationColor; note: string }) => void
  onDelete: () => void
  onClose: () => void
}) {
  const [color, setColor] = useState<AnnotationColor>(annotation.color)
  const [note, setNote] = useState(annotation.note)

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement
      if (target.closest('.hl-editor')) return
      onClose()
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [onClose])

  const width = 300
  const left = Math.min(
    window.innerWidth - width - 12,
    Math.max(12, rect.left + rect.width / 2 - width / 2),
  )
  const top = Math.min(window.innerHeight - 220, rect.bottom + 8)

  return (
    <div className="hl-editor" style={{ left, top, width }} onMouseDown={(e) => e.stopPropagation()}>
      <div className="hl-editor-head">
        <span className="hl-editor-quote" title={annotation.quote}>
          “{annotation.quote}”
        </span>
        <button type="button" className="icon-btn" onClick={onClose} title="关闭">
          ×
        </button>
      </div>
      <div className="hl-editor-colors">
        {HL_COLORS.map((c) => (
          <button
            key={c.key}
            type="button"
            className={`hl-dot hl-${c.key}`}
            data-active={color === c.key}
            title={c.label}
            onClick={() => setColor(c.key)}
          />
        ))}
      </div>
      <textarea
        className="hl-editor-note"
        value={note}
        placeholder="写点笔记…（留空则只保留高亮）"
        onChange={(e) => setNote(e.target.value)}
        rows={3}
      />
      <div className="hl-editor-actions">
        <button type="button" className="link-btn danger" onClick={onDelete}>
          删除
        </button>
        <button
          type="button"
          className="btn small primary"
          onClick={() => onSave({ color, note })}
        >
          保存
        </button>
      </div>
    </div>
  )
}
