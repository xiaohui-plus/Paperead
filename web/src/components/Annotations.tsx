import {
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from 'react'
import type {
  Annotation,
  AnnotationColor,
  AnnotationKind,
  AnnotationPair,
  HighlightCategory,
  PdfRect,
  ReadingHighlight,
} from '../lib/library'
import { lastAnnotationColor, rememberAnnotationColor } from '../lib/annotationPrefs'
import { IconComment, IconHighlighter } from './icons'

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
    block_id?: string
    source: 'en' | 'zh'
    start?: number
    end?: number
    quote: string
    color: AnnotationColor
    kind: AnnotationKind
    pair?: AnnotationPair | null
    /** 原版 PDF 标注：页码 + 页内归一化矩形 */
    page?: number
    rects?: PdfRect[]
  }) => Promise<Annotation | void>
  update: (
    id: string,
    patch: { color?: AnnotationColor; note?: string; pair?: AnnotationPair },
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
  /** 这条标注原本画在另一侧，这里是同步过来的对应片段 */
  mirror?: boolean
  term?: string
  highlight?: ReadingHighlight
}

/**
 * 把原文/译文切成互不重叠的片段：引用编号优先，其次用户高亮、自动重点，最后术语。
 * 同一条标注如果记录了另一侧片段，也会画在那一侧（原文高亮自动同步到译文）。
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
    let start: number
    let end: number
    let quote: string
    let mirror = false
    if (annotation.source === source) {
      start = annotation.start
      end = annotation.end
      quote = annotation.quote
    } else if (annotation.pair && annotation.pair.source === source) {
      start = annotation.pair.start
      end = annotation.pair.end
      quote = annotation.pair.quote
      mirror = true
    } else {
      continue
    }
    // 重新解析或重译后偏移可能失配：按原文片段重新定位，找不到就不画
    if (text.slice(start, end) !== quote) {
      const found = quote ? text.indexOf(quote) : -1
      if (found < 0) continue
      start = found
      end = found + quote.length
    }
    if (end > start) spans.push({ start, end, kind: 'hl', annotation, mirror })
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
  /** 双击正文里的高亮/批注：切到左侧批注栏并定位到这一条 */
  onAnnotationOpen?: (annotation: Annotation) => void
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
  onAnnotationOpen,
}: RichProps) {
  const spans = buildSpans(text, source, annotations, highlights, terms)
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
      const spanText = text.slice(span.start, span.end)
      const isComment = annotation.kind === 'comment'
      const isMirror = Boolean(span.mirror)
      const ownLabel = annotation.source === 'en' ? '原文' : '译文'
      const title = isMirror
        ? `${ownLabel}上的${isComment ? '批注' : '荧光笔'}，这里是同步对应内容`
        : annotation.note || (isComment ? '点击编辑这条批注' : '点击编辑这条高亮')
      const onPick = (e: ReactMouseEvent<HTMLElement>) => {
        e.stopPropagation()
        // 双击的第二下不再弹编辑框，交给 onDoubleClick 切到批注栏
        if (e.detail > 1) return
        onAnnotationClick?.(annotation, (e.target as HTMLElement).getBoundingClientRect())
      }
      const onOpen = (e: ReactMouseEvent<HTMLElement>) => {
        e.stopPropagation()
        onAnnotationOpen?.(annotation)
      }
      nodes.push(
        isComment ? (
          <span
            key={`h${span.start}`}
            className={`cm cm-${annotation.color}${isMirror ? ' cm-pair' : ''}`}
            data-aid={annotation.id}
            data-pair={isMirror ? '1' : undefined}
            title={title}
            onClick={onPick}
            onDoubleClick={onOpen}
          >
            {spanText}
          </span>
        ) : (
          <mark
            key={`h${span.start}`}
            className={`hl hl-${annotation.color}${isMirror ? ' hl-pair' : ''}`}
            data-aid={annotation.id}
            data-pair={isMirror ? '1' : undefined}
            title={title}
            onClick={onPick}
            onDoubleClick={onOpen}
          >
            {spanText}
          </mark>
        ),
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

/** 划词后浮在选区上方的工具条：两个图标，点击用上次颜色，悬停展开调色板。 */
export function SelectionToolbar({
  rect,
  onPick,
  onComment,
  onClose,
}: {
  rect: DOMRect
  onPick: (color: AnnotationColor) => void
  onComment: (color: AnnotationColor) => void
  onClose: () => void
}) {
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (!(event.target as HTMLElement).closest('.hl-toolbar')) onClose()
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [onClose])

  const [hlColor, setHlColor] = useState(() => lastAnnotationColor('highlight'))
  const [cmColor, setCmColor] = useState(() => lastAnnotationColor('comment'))

  const apply = (kind: 'highlight' | 'comment', color: AnnotationColor) => {
    rememberAnnotationColor(kind, color)
    if (kind === 'highlight') {
      setHlColor(color)
      onPick(color)
    } else {
      setCmColor(color)
      onComment(color)
    }
  }

  const width = 104
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
      <ToolAction
        icon={<IconHighlighter />}
        label="荧光笔"
        color={hlColor}
        onUse={() => apply('highlight', hlColor)}
        onChoose={(color) => apply('highlight', color)}
      />
      <ToolAction
        icon={<IconComment />}
        label="批注"
        color={cmColor}
        onUse={() => apply('comment', cmColor)}
        onChoose={(color) => apply('comment', color)}
      />
    </div>
  )
}

/** 单个工具：图标按钮直接使用上次颜色，悬停/聚焦时下方展开四个色点。 */
function ToolAction({
  icon,
  label,
  color,
  onUse,
  onChoose,
}: {
  icon: ReactNode
  label: string
  color: AnnotationColor
  onUse: () => void
  onChoose: (color: AnnotationColor) => void
}) {
  const colorLabel = HL_COLORS.find((c) => c.key === color)?.label ?? ''
  return (
    <div className="hl-tool">
      <button
        type="button"
        className="hl-tool-btn"
        title={`${label}：直接点击用上次颜色（${colorLabel}），悬停可选色`}
        aria-label={label}
        onClick={onUse}
      >
        {icon}
        <span className={`hl-tool-current hl-${color}`} aria-hidden />
      </button>
      <div className="hl-tool-palette" role="menu" aria-label={`${label}颜色`}>
        {HL_COLORS.map((c) => (
          <button
            key={c.key}
            type="button"
            className={`hl-dot hl-${c.key}`}
            data-active={c.key === color}
            title={c.label}
            aria-label={`${label} ${c.label}`}
            onClick={() => onChoose(c.key)}
          />
        ))}
      </div>
    </div>
  )
}

/** 点击已有高亮/批注：改颜色、写内容或删除。 */
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
  const isComment = annotation.kind === 'comment'

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
        <span className="hl-editor-kind">{isComment ? '批注' : '荧光笔'}</span>
        <span className="hl-editor-quote" title={annotation.quote}>
          “{annotation.quote}”
        </span>
        <button
          type="button"
          className="icon-btn"
          onClick={onClose}
          title={isComment ? '关闭' : '关闭（荧光笔保留）'}
        >
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
        autoFocus
        placeholder={
          isComment ? '写下你的批注…' : '写点笔记…（可不填，直接关闭也保留荧光笔）'
        }
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
