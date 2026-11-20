import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
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
import type { InlineMath } from '../types'
import katex from 'katex'
import { mediaUrl } from '../lib/mobile'
import { lastAnnotationColor, rememberAnnotationColor } from '../lib/annotationPrefs'
import { CITE_RE, hasCitation } from '../lib/citationMark'
import { stripDotLeaders } from '../lib/text'
import { normalizeMath } from '../lib/math'
import { translateText } from '../lib/library'
import { readyModel, loadModelSettings, toPayload } from '../lib/settings'
import { langLabel, loadTargetLang } from '../lib/translation'
import { canRunLocally, serverReachable } from '../lib/connectivity'
import { streamLocalChat } from '../lib/localLlm'
import {
  IconChat,
  IconComment,
  IconHighlighter,
  IconPin,
  IconPinOff,
  IconTranslate,
} from './icons'
import { MathText } from './MathText'

/** 自动重点的中文名，正文 tooltip 和「阅读重点」面板共用。 */
export const HIGHLIGHT_LABELS: Record<HighlightCategory, string> = {
  objective: '研究目标',
  method: '研究方法',
  conclusion: '主要结论',
}

export { CITE_RE, hasCitation }

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

  // 同一条标注在每一侧只画一次：数据里若出现重复条目（旧数据合并、重复提交），
  // 会出现两个 <span class="cm"> 叠在同一段文字上，视觉上就是两层下划线。
  const seen = new Set<string>()

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
    const sideKey = `${annotation.id}:${mirror ? 'pair' : 'own'}`
    if (seen.has(sideKey)) continue
    seen.add(sideKey)
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

/** 正文行内公式：优先 KaTeX，规则无法重建时显示解析器裁的小图。 */
function InlineMathNode({ item }: { item: InlineMath }) {
  if (item.image_path) {
    return <img className="inline-math-img" src={mediaUrl(item.image_path)} alt="" />
  }
  if (!item.latex) return null
  return (
    <span
      className="inline-math"
      dangerouslySetInnerHTML={{
        __html: katex.renderToString(item.latex, {
          throwOnError: false,
          strict: false,
          displayMode: false,
        }),
      }}
    />
  )
}

interface RichProps {
  text: string
  source: 'en' | 'zh'
  annotations: Annotation[]
  highlights?: ReadingHighlight[]
  terms?: Record<string, string>
  inlineMath?: InlineMath[]
  className: string
  onCite?: (marker: string, rect: DOMRect, el: HTMLElement) => void
  onAnnotationClick?: (annotation: Annotation, rect: DOMRect) => void
  /** 双击正文里的高亮/批注：切到左侧批注栏并定位到这一条 */
  onAnnotationOpen?: (annotation: Annotation) => void
  /** 双击自动阅读重点：切到左侧「阅读重点」并定位到该条 */
  onHighlightOpen?: (highlight: ReadingHighlight) => void
}

/** 正文段落：把引用编号、用户高亮、自动重点、术语标进同一段文本。 */
export function RichText({
  text: rawText,
  source,
  annotations,
  highlights = [],
  terms = {},
  inlineMath = [],
  className,
  onCite,
  onAnnotationClick,
  onAnnotationOpen,
  onHighlightOpen,
}: RichProps) {
  // 历史 blocks.json 的目录条目里残留点线引导符；行内公式按字符偏移渲染，
  // 只有没有公式时才能安全裁剪文本。
  const text =
    source === 'zh' || inlineMath.length === 0 ? stripDotLeaders(rawText) : rawText
  const formulas = inlineMath
    .filter((m) => (m.latex || m.image_path) && m.start >= 0 && m.end > m.start)
    .sort((a, b) => a.start - b.start)
  const spans = buildSpans(text, source, annotations, highlights, terms).filter(
    (span) => !formulas.some((m) => span.start < m.end && span.end > m.start),
  )
  const nodes: ReactNode[] = []
  let cursor = 0

  // 输出一段普通文本，遇到行内公式时换成 KaTeX / 小图。
  // 与公式重叠的批注/高亮在上面已经过滤掉，所以这里不会切开一个公式。
  const renderWithMath = (from: number, to: number): ReactNode[] => {
    const out: ReactNode[] = []
    let pos = from
    for (const m of formulas) {
      if (m.end <= pos) continue
      if (m.start >= to) break
      if (m.start > pos) {
        out.push(<span key={`t${pos}`}>{text.slice(pos, m.start)}</span>)
      }
      const start = Math.max(m.start, from)
      const end = Math.min(m.end, to)
      if (start === m.start && end === m.end) {
        out.push(<InlineMathNode key={`m${m.start}`} item={m} />)
      } else if (end > start) {
        out.push(<span key={`t${start}`}>{text.slice(start, end)}</span>)
      }
      pos = end
    }
    if (pos < to) out.push(<span key={`t${pos}`}>{text.slice(pos, to)}</span>)
    return out
  }
  // 悬停稍等一下再开预览，划词或快速划过整段时不会频繁弹层
  const hoverTimer = useRef<number | undefined>(undefined)
  // 单击编辑框延迟打开：双击时由 onOpen 取消，避免切到批注栏后正文下方又弹一个编辑框
  const clickTimer = useRef<number | undefined>(undefined)
  const pendingClick = useRef<{ annotation: Annotation; rect: DOMRect } | null>(null)
  const cancelHover = () => {
    if (hoverTimer.current) {
      window.clearTimeout(hoverTimer.current)
      hoverTimer.current = undefined
    }
  }
  useEffect(() => {
    return () => {
      cancelHover()
      if (clickTimer.current) window.clearTimeout(clickTimer.current)
    }
  }, [])

  for (const span of spans) {
    if (span.start > cursor) {
      nodes.push(...renderWithMath(cursor, span.start))
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
        // 双击的第二下不再开编辑框，交给 onDoubleClick 切到批注栏
        if (e.detail > 1) return
        pendingClick.current = {
          annotation,
          rect: (e.target as HTMLElement).getBoundingClientRect(),
        }
        if (clickTimer.current) window.clearTimeout(clickTimer.current)
        clickTimer.current = window.setTimeout(() => {
          clickTimer.current = undefined
          const pending = pendingClick.current
          pendingClick.current = null
          if (pending) onAnnotationClick?.(pending.annotation, pending.rect)
        }, 200)
      }
      const onOpen = (e: ReactMouseEvent<HTMLElement>) => {
        e.stopPropagation()
        // 双击：取消待打开的编辑框，只让左侧批注栏定位到这一条
        if (clickTimer.current) {
          window.clearTimeout(clickTimer.current)
          clickTimer.current = undefined
        }
        pendingClick.current = null
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
      const highlight = span.highlight
      const category = highlight.category
      nodes.push(
        <mark
          key={`a${span.start}`}
          className={`auto-hl auto-${category}`}
          data-hid={highlight.id}
          title={`自动标记：${HIGHLIGHT_LABELS[category]}（双击查看阅读重点）`}
          onDoubleClick={(e) => {
            e.stopPropagation()
            onHighlightOpen?.(highlight)
          }}
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
  if (cursor < text.length) nodes.push(...renderWithMath(cursor, text.length))

  return (
    <p className={className} data-text-source={source}>
      {nodes}
    </p>
  )
}

/**
 * 阅读页整体按 120% 缩放（`html { zoom }`）。
 *
 * `getBoundingClientRect()` 返回的是缩放后的视觉坐标，而 `position: fixed` 的
 * left/top 是布局坐标（渲染时还会乘一次 zoom）。两者直接混用会让浮层按缩放比
 * 向右下漂：1.2 缩放下选区中心偏 132px 是常态，越靠右下偏得越多。
 */
export function uiZoom(): number {
  if (typeof window === 'undefined') return 1
  const value = Number(getComputedStyle(document.documentElement).zoom)
  return Number.isFinite(value) && value > 0 ? value : 1
}

/** 视觉坐标（getBoundingClientRect）→ 布局坐标，供 fixed 浮层定位使用。 */
export function layoutRect(rect: DOMRect, zoom = uiZoom()): DOMRect {
  if (zoom === 1) return rect
  const left = rect.left / zoom
  const top = rect.top / zoom
  const width = rect.width / zoom
  const height = rect.height / zoom
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({ x: left, y: top, left, top, width, height, right: left + width, bottom: top + height }),
  } as DOMRect
}

function clampPosition(value: number, min: number, max: number): number {
  if (max < min) return Math.max(0, max)
  return Math.min(Math.max(value, min), max)
}

/** 可交互/可选中区域不参与拖动，避免抢掉点击、输入与复制译文。 */
const DRAG_IGNORE = 'button, a, input, textarea, select, .hl-translate-text'

/**
 * 浮层拖动：与划词工具条同一套规则（4px 阈值、zoom 换算、视口钳制、拖完抑制 click）。
 *
 * 翻译卡片与笔记编辑框默认贴在选区/工具条旁，只有用户真正按住拖动后才改用拖拽位置；
 * `resetKey` 变化（换选区、换标注）时回到自动定位，避免沿用上一次的位置。
 */
function useFloatDrag(
  base: { left: number; top: number; width: number },
  resetKey?: string,
  options?: {
    /** 固定到 PDF 内容时：left/top 是文档坐标，随页面滚动，不钳制在视口内 */
    documentMode?: boolean
    onDragEnd?: (pos: { left: number; top: number }) => void
  },
) {
  const ref = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ width: base.width, height: 0 })
  const [dragPos, setDragPos] = useState<{ left: number; top: number } | null>(null)
  const [dragging, setDragging] = useState(false)
  const documentMode = Boolean(options?.documentMode)

  // 卡片高度会随内容变化（骨架 → 译文、译文长短、textarea 拉伸），用实测值做边界钳制
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const next = { width: el.offsetWidth, height: el.offsetHeight }
    setSize((prev) => (prev.width === next.width && prev.height === next.height ? prev : next))
  })

  useEffect(() => {
    setDragPos(null)
    setDragging(false)
  }, [resetKey])

  const dragRef = useRef<{
    pointerId: number
    startX: number
    startY: number
    baseLeft: number
    baseTop: number
    moved: boolean
  } | null>(null)
  const posRef = useRef<{ left: number; top: number } | null>(null)
  const suppressClick = useRef(false)

  const zoom = uiZoom()
  const pad = VIEWPORT_PAD / zoom
  const width = size.width || base.width
  const height = size.height
  const left = dragPos?.left ?? base.left
  const top = dragPos?.top ?? base.top
  posRef.current = { left, top }
  // 钉在内容上时卡片属于文档流，不应被视口边界钳制；拖动范围交给页面本身
  const maxLeft = documentMode
    ? Number.MAX_SAFE_INTEGER
    : Math.max(pad, window.innerWidth / zoom - width - pad)
  const maxTop = documentMode
    ? Number.MAX_SAFE_INTEGER
    : Math.max(pad, window.innerHeight / zoom - height - pad)

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const target = event.target as HTMLElement
    if (target.closest(DRAG_IGNORE)) return
    // 非交互区按下时阻止文本选区；点击仍会正常补发，不受影响
    event.preventDefault()
    suppressClick.current = false
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      baseLeft: left,
      baseTop: top,
      moved: false,
    }
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const dx = (event.clientX - drag.startX) / zoom
    const dy = (event.clientY - drag.startY) / zoom
    if (!drag.moved && Math.hypot(dx, dy) < 4) return
    if (!drag.moved) {
      drag.moved = true
      suppressClick.current = true
      setDragging(true)
      try {
        event.currentTarget.setPointerCapture(event.pointerId)
      } catch {
        // 指针已失效时忽略，下一次 move 会继续拖动
      }
    }
    setDragPos({
      left: clampPosition(drag.baseLeft + dx, pad, maxLeft),
      top: clampPosition(drag.baseTop + dy, pad, maxTop),
    })
  }

  const onPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    setDragging(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    const final = posRef.current
    if (final) options?.onDragEnd?.(final)
  }

  // 拖拽结束浏览器会补发 click，不能让它落到「复制译文 / 保存」等按钮上
  const onClickCapture = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!suppressClick.current) return
    suppressClick.current = false
    event.preventDefault()
    event.stopPropagation()
  }

  return {
    ref,
    dragging,
    left,
    top,
    setDragPos,
    handlers: {
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onPointerCancel: onPointerUp,
      onClickCapture,
    },
  }
}

interface TranslateState {
  status: 'idle' | 'loading' | 'done' | 'error'
  /** 本机流式翻译时已到达的部分译文 */
  partial?: string
  value?: string
  message?: string
}

const TOOLBAR_HEIGHT = 38
const FLOAT_GAP = 10
const VIEWPORT_PAD = 12

/** 划词后浮在选区上方的工具条：荧光笔 / 批注 / 翻译，有提问入口时再多一个「提问」。 */
export function SelectionToolbar({
  rect,
  text,
  onPick,
  onComment,
  onAsk,
  onClose,
}: {
  rect: DOMRect
  /** 选中的原文；有值时提供划词翻译 */
  text?: string
  onPick: (color: AnnotationColor) => void
  onComment: (color: AnnotationColor) => void
  /** 把选中的文字作为引用带到左侧问答面板 */
  onAsk?: () => void
  onClose: () => void
}) {
  const [pinned, setPinned] = useState(false)

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      // 固定到 PDF 内容后不能再被点的别处顺手关掉；只允许卡片上的关闭按钮结束。
      if (pinned) return
      const target = event.target as HTMLElement
      // 翻译卡片属于同一个浮层，点它内部不关闭
      if (!target.closest('.hl-toolbar, .hl-translate')) onClose()
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [onClose, pinned])

  const [hlColor, setHlColor] = useState(() => lastAnnotationColor('highlight'))
  const [cmColor, setCmColor] = useState(() => lastAnnotationColor('comment'))
  const [translation, setTranslation] = useState<TranslateState>({ status: 'idle' })
  const targetName = langLabel(loadTargetLang())

  useEffect(() => {
    if (translation.status === 'idle') return
    const onKey = (event: KeyboardEvent) => {
      // 固定后只有主动关闭；ESC 不再自动消失
      if (event.key === 'Escape' && !pinned) setTranslation({ status: 'idle' })
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [translation.status, pinned])

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

  /** 本机模型通道：手机壳/离线时逐段回填，交互与整篇翻译一致。 */
  const translateLocally = async (source: string, lang: string) => {
    const cfg = readyModel(loadModelSettings(), 'answer')
    if (!cfg) throw new Error('还没有配置回答模型。请到「设置 → 模型」填写模型与 API Key 后再试。')
    let acc = ''
    const full = await streamLocalChat({
      config: { baseUrl: cfg.baseUrl, apiKey: cfg.apiKey, model: cfg.model },
      messages: [
        {
          role: 'system' as const,
          content:
            `你是学术论文翻译专家，把用户选中的文字翻译成${langLabel(lang)}。` +
            '只输出译文正文，不要解释、不要加引号或 Markdown 代码块。' +
            '数学公式、引用编号与 URL 请原样保留，不要改写。',
        },
        { role: 'user' as const, content: stripDotLeaders(source) },
      ],
      temperature: 0.2,
      maxTokens: 1200,
      onDelta: (delta) => {
        acc += delta
        setTranslation({ status: 'loading', partial: acc })
      },
    })
    const value = stripDotLeaders(full).trim()
    if (!value) throw new Error('模型没有返回译文')
    setTranslation({ status: 'done', value })
  }

  const translate = async () => {
    const source = text?.trim()
    if (!source) return
    setTranslation({ status: 'loading' })
    const cfg = readyModel(loadModelSettings(), 'answer')
    if (!cfg) {
      setTranslation({
        status: 'error',
        message: '还没有配置回答模型。请到「设置 → 模型」填写模型与 API Key 后再试。',
      })
      return
    }
    const lang = loadTargetLang()
    try {
      const online = await serverReachable()
      if (online) {
        try {
          const result = await translateText(source, toPayload(cfg), lang)
          setTranslation({ status: 'done', value: result.translation })
          return
        } catch (err) {
          // 服务端翻译失败但本机通道可用时再兜底一次
          if (!canRunLocally()) throw err
        }
      } else if (!canRunLocally()) {
        throw new Error('连不上本地服务，请确认服务已启动后重试。')
      }
      await translateLocally(source, lang)
    } catch (err) {
      const message = err instanceof Error ? err.message : '翻译失败，请稍后重试'
      setTranslation({ status: 'error', message })
    }
  }

  const toolbarRef = useRef<HTMLDivElement>(null)
  const [toolbarWidth, setToolbarWidth] = useState(0)
  // 按钮数量会随选区变化（翻译/提问按需出现），宽度必须量实际元素，
  // 用常量估算会让工具条偏离选区中心。
  useLayoutEffect(() => {
    const el = toolbarRef.current
    if (!el) return
    const next = el.offsetWidth
    setToolbarWidth((prev) => (prev === next ? prev : next))
  })
  const width = toolbarWidth
  const zoom = uiZoom()
  // 选区 rect 是视觉坐标，先换算回布局坐标，否则 fixed 浮层会按 zoom 偏向右下
  const anchorRect = layoutRect(rect, zoom)

  const pad = VIEWPORT_PAD / zoom

  const baseLeft = clampPosition(
    anchorRect.left + anchorRect.width / 2 - width / 2,
    pad,
    window.innerWidth / zoom - width - pad,
  )
  const below = anchorRect.bottom + FLOAT_GAP / zoom
  const maxTop = window.innerHeight / zoom - TOOLBAR_HEIGHT - pad
  const baseTop =
    below <= maxTop
      ? below
      : clampPosition(
          anchorRect.top - TOOLBAR_HEIGHT - FLOAT_GAP / zoom,
          pad,
          maxTop,
        )

  const [dragPos, setDragPos] = useState<{ left: number; top: number } | null>(null)
  const [dragging, setDragging] = useState(false)
  // 同一段落里连续划词时组件不会卸载：上一次拖到别处的位置必须跟着新选区失效，
  // 否则新工具条仍停在上次的右下角，看起来像「每次都飘到很远的地方」。
  const rectKey = `${rect.left}:${rect.top}:${rect.width}:${rect.height}`
  useEffect(() => {
    setDragPos(null)
    setDragging(false)
  }, [rectKey])
  const dragRef = useRef<{
    pointerId: number
    startX: number
    startY: number
    baseLeft: number
    baseTop: number
    moved: boolean
  } | null>(null)
  const suppressClick = useRef(false)
  const left = dragPos?.left ?? baseLeft
  const top = dragPos?.top ?? baseTop

  const onDragStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const target = event.target as HTMLElement
    // 色板里点颜色不进入拖拽
    if (target.closest('.hl-tool-palette, .hl-dot')) return
    suppressClick.current = false
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      baseLeft: left,
      baseTop: top,
      moved: false,
    }
    // 这里不能立刻 setPointerCapture：捕获会把后续 click 重定向到工具条容器，
    // 按钮就收不到点击。等真正拖动（超过阈值）时再捕获。
  }

  const onDragMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    // 鼠标位移是视觉像素，换算回 zoom 前的布局像素
    const dx = (event.clientX - drag.startX) / zoom
    const dy = (event.clientY - drag.startY) / zoom
    if (!drag.moved && Math.hypot(dx, dy) < 4) return
    if (!drag.moved) {
      drag.moved = true
      suppressClick.current = true
      setDragging(true)
      try {
        event.currentTarget.setPointerCapture(event.pointerId)
      } catch {
        // 指针已失效时忽略，拖动仍会随下一次 move 继续
      }
    }
    setDragPos({
      left: clampPosition(
        drag.baseLeft + dx,
        pad,
        window.innerWidth / zoom - width - pad,
      ),
      top: clampPosition(drag.baseTop + dy, pad, maxTop),
    })
  }

  const onDragEnd = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    setDragging(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }

  // 拖拽结束会补发一次 click，不能让它落到某个按钮上误建标注
  const onClickCapture = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!suppressClick.current) return
    suppressClick.current = false
    event.preventDefault()
    event.stopPropagation()
  }

  return (
    <>
      {!pinned && (
      <div
        ref={toolbarRef}
        className={`hl-toolbar${dragging ? ' is-dragging' : ''}`}
        style={{ left, top }}
        onMouseDown={(e) => e.preventDefault()}
        onPointerDown={onDragStart}
        onPointerMove={onDragMove}
        onPointerUp={onDragEnd}
        onPointerCancel={onDragEnd}
        onClickCapture={onClickCapture}
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
        {text && (
          <button
            type="button"
            className="hl-tool-btn hl-translate-btn"
            title="翻译选中的文字"
            aria-label="翻译选中的文字"
            onClick={() => void translate()}
          >
            <IconTranslate />
          </button>
        )}
        {onAsk && (
          <button
            type="button"
            className="hl-tool-btn hl-ask-btn"
            title="就选中的这段文字提问"
            aria-label="就选中的这段文字提问"
            onClick={onAsk}
          >
            <IconChat />
          </button>
        )}
      </div>
      )}
      {translation.status !== 'idle' && (
        <TranslateCard
          anchor={{ left, top, width }}
          state={translation}
          target={targetName}
          resetKey={rectKey}
          pinned={pinned}
          onTogglePin={setPinned}
          onClose={() => {
            setTranslation({ status: 'idle' })
            setPinned(false)
          }}
          onRetry={() => void translate()}
        />
      )}
    </>
  )
}

/** 划词翻译结果卡片：刻意用统一浅底 + 细线，颜色只落在语言标签与操作文字上。 */
function TranslateCard({
  anchor,
  state,
  target,
  resetKey,
  pinned,
  onTogglePin,
  onClose,
  onRetry,
}: {
  /** 工具条当前所在位置：卡片默认贴在工具条下方，工具条拖动后跟随 */
  anchor: { left: number; top: number; width: number }
  state: TranslateState
  target: string
  /** 选区变化时丢弃手动拖到的位置 */
  resetKey?: string
  /** 固定到 PDF 内容：卡片停在此处，随页面滚动上移；不再自动消失 */
  pinned: boolean
  onTogglePin: (next: boolean) => void
  onClose: () => void
  onRetry: () => void
}) {
  const [copied, setCopied] = useState(false)
  const zoom = uiZoom()
  const width = Math.min(328, window.innerWidth / zoom - (VIEWPORT_PAD * 2) / zoom)
  const pad = VIEWPORT_PAD / zoom
  const left = clampPosition(
    anchor.left + (anchor.width - width) / 2,
    pad,
    window.innerWidth / zoom - width - pad,
  )
  // 成果卡片高度随内容变化，这里用保守估计决定放下方还是翻到上方
  const estimated = state.status === 'done' ? 240 : 140
  const below = anchor.top + TOOLBAR_HEIGHT + FLOAT_GAP / zoom
  const maxTop = window.innerHeight / zoom - estimated - pad
  const top =
    below <= maxTop
      ? below
      : clampPosition(anchor.top - estimated - FLOAT_GAP / zoom, pad, maxTop)

  const [pinBase, setPinBase] = useState<{ left: number; top: number } | null>(null)
  const [scrollShift, setScrollShift] = useState(0)
  const pinRef = useRef<{ top: number; scroll: number; scroller: HTMLElement | null } | null>(null)

  const base = pinned && pinBase ? { ...pinBase, width } : { left, top, width }
  const drag = useFloatDrag(base, pinned ? 'pinned' : resetKey, {
    onDragEnd: (pos) => {
      if (!pinned) return
      setPinBase({ left: pos.left, top: pos.top })
      const scroller = pinRef.current?.scroller ?? null
      if (scroller) pinRef.current = { top: pos.top, scroll: scroller.scrollTop, scroller }
      setScrollShift(0)
    },
  })

  const findScroller = (node: HTMLElement | null): HTMLElement | null => {
    // PDF 模式的滚动容器 `.pdf-view` 与划词浮层不在同一棵子树里（浮层是
    // 页面级浮层），只向上找 parentElement 会漏掉它；先按已知容器找。
    for (const selector of ['.pdf-view', '.main-scroll']) {
      const candidate = document.querySelector<HTMLElement>(selector)
      if (
        candidate &&
        /(auto|scroll)/.test(window.getComputedStyle(candidate).overflowY) &&
        candidate.scrollHeight > candidate.clientHeight
      ) {
        return candidate
      }
    }
    let current = node?.parentElement ?? null
    while (current) {
      const style = window.getComputedStyle(current)
      if (/(auto|scroll)/.test(style.overflowY) && current.scrollHeight > current.clientHeight) {
        return current
      }
      current = current.parentElement
    }
    return null
  }

  // 阅读页的滚动发生在 `.main-scroll` 内部，不是 window。固定后监听真实
  // 滚动容器，按滚动量把卡片往上推，视觉上就与 PDF 正文一起滑动。
  useEffect(() => {
    if (!pinned) {
      setScrollShift(0)
      return
    }
    const scroller = pinRef.current?.scroller ?? findScroller(drag.ref.current)
    if (!scroller) return
    if (!pinRef.current) {
      pinRef.current = { top: drag.top, scroll: scroller.scrollTop, scroller }
    }
    const onScroll = () => {
      const baseInfo = pinRef.current
      if (!baseInfo) return
      setScrollShift(scroller.scrollTop - baseInfo.scroll)
    }
    scroller.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => scroller.removeEventListener('scroll', onScroll)
  }, [pinned, zoom, drag.ref, drag.top])

  const togglePin = () => {
    if (pinned) {
      setPinBase(null)
      pinRef.current = null
      setScrollShift(0)
      onTogglePin(false)
      return
    }
    const scroller = findScroller(drag.ref.current)
    pinRef.current = scroller
      ? { top: drag.top, scroll: scroller.scrollTop, scroller }
      : null
    setScrollShift(0)
    setPinBase({ left: drag.left, top: drag.top })
    onTogglePin(true)
  }

  const copy = async () => {
    const value = state.value
    if (state.status !== 'done' || !value) return
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value)
      } else {
        const area = document.createElement('textarea')
        area.value = value
        area.style.position = 'fixed'
        area.style.opacity = '0'
        document.body.appendChild(area)
        area.select()
        document.execCommand('copy')
        area.remove()
      }
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1400)
    } catch {
      // 复制失败不打断阅读，用户仍可手动选中译文
    }
  }

  const renderedTop = drag.top - (pinned ? scrollShift : 0)

  return (
    <div
      ref={drag.ref}
      className={`hl-translate${drag.dragging ? ' is-dragging' : ''}${pinned ? ' is-pinned' : ''}`}
      style={{ left: drag.left, top: renderedTop, width }}
      onMouseDown={(e) => e.stopPropagation()}
      {...drag.handlers}
    >
      <div className="hl-translate-head">
        <span className="hl-translate-lang">EN → {target}</span>
        <span className="hl-translate-head-actions">
          <button
            type="button"
            className={`icon-btn hl-translate-pin${pinned ? ' is-active' : ''}`}
            title={pinned ? '取消固定' : '固定到 PDF 内容（随页面滚动）'}
            aria-label={pinned ? '取消固定' : '固定翻译卡片'}
            aria-pressed={pinned}
            onClick={togglePin}
          >
            {pinned ? <IconPinOff /> : <IconPin />}
          </button>
          <button type="button" className="icon-btn" title="关闭翻译" onClick={onClose}>
            ×
          </button>
        </span>
      </div>

      {state.status === 'loading' && !state.partial && (
        <div className="hl-translate-skeleton" aria-label="正在翻译">
          <span />
          <span />
          <span />
        </div>
      )}
      {state.status === 'loading' && state.partial && (
        <p className="hl-translate-text">
          {state.partial}
          <span className="hl-translate-caret" aria-hidden />
        </p>
      )}
      {state.status === 'done' && (
        <p className="hl-translate-text hl-translate-math">
          <MathText
            text={normalizeMath(state.value ?? '')}
            renderText={(text) => <span className="hl-translate-plain">{text}</span>}
          />
        </p>
      )}
      {state.status === 'error' && (
        <p className="hl-translate-error">
          {state.message}
          <button type="button" className="link-btn" onClick={onRetry}>
            重试
          </button>
        </p>
      )}

      {state.status === 'done' && (
        <div className="hl-translate-actions">
          <span className="hl-translate-note">划词翻译</span>
          <button type="button" className="link-btn" onClick={() => void copy()}>
            {copied ? '已复制' : '复制译文'}
          </button>
        </div>
      )}
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
  autoFocus = true,
  onSave,
  onDelete,
  onClose,
}: {
  annotation: Annotation
  rect: DOMRect
  /** 新建标注时自动聚焦输入；单击已有标注查看时不抢焦点 */
  autoFocus?: boolean
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

  const zoom = uiZoom()
  const anchorRect = layoutRect(rect, zoom)
  const width = Math.min(300, window.innerWidth / zoom - (VIEWPORT_PAD * 2) / zoom)
  const pad = VIEWPORT_PAD / zoom
  const left = clampPosition(
    anchorRect.left + anchorRect.width / 2 - width / 2,
    pad,
    window.innerWidth / zoom - width - pad,
  )
  const top = clampPosition(
    anchorRect.bottom + 8 / zoom,
    pad,
    window.innerHeight / zoom - 220 - pad,
  )

  const drag = useFloatDrag(
    { left, top, width },
    `${annotation.id}:${rect.left}:${rect.top}:${rect.width}:${rect.height}`,
  )

  return (
    <div
      ref={drag.ref}
      className={`hl-editor${drag.dragging ? ' is-dragging' : ''}`}
      style={{ left: drag.left, top: drag.top, width }}
      onMouseDown={(e) => e.stopPropagation()}
      {...drag.handlers}
    >
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
        autoFocus={autoFocus}
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
