import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import type { Block, ReaderContextItem, ViewMode } from '../types'
import type { RenderBlock } from '../lib/blocks'
import {
  getProvenance,
  type Annotation,
  type AnnotationColor,
  type ProvenanceCitation,
  type ReadingHighlight,
} from '../lib/library'
import {
  AnnotationPopover,
  RichText,
  SelectionToolbar,
  hasCitation,
  type AnnotationActions,
} from './Annotations'
import { Katex } from './Katex'
import { IconCheck, IconClose, IconCopy, IconPlus } from './icons'
import { configBody, visionModel } from '../lib/settings'

interface Props {
  rb: RenderBlock
  mode: ViewMode
  terms: Record<string, string>
  /** 整篇是否有任何译文：没有的话不显示骨架条，避免整页都在转圈 */
  hasTranslation: boolean
  paperId?: string
  annotations?: Annotation[]
  annotationActions?: AnnotationActions
  highlights?: ReadingHighlight[]
  /** 已加入左侧问答附录的 block id */
  attachedIds?: Set<string>
  onAttachContext?: (item: ReaderContextItem) => void
}

interface SelectionState {
  source: 'en' | 'zh'
  start: number
  end: number
  quote: string
  rect: DOMRect
}

export function BlockView({
  rb,
  mode,
  terms,
  hasTranslation,
  paperId,
  annotations = [],
  annotationActions,
  highlights = [],
  attachedIds,
  onAttachContext,
}: Props) {
  const b = rb.block
  const [copied, setCopied] = useState(false)
  const [provOpen, setProvOpen] = useState(false)
  const [prov, setProv] = useState<ProvenanceCitation[] | null>(null)
  const [provBusy, setProvBusy] = useState(false)
  const [selection, setSelection] = useState<SelectionState | null>(null)
  const [editing, setEditing] = useState<{ annotation: Annotation; rect: DOMRect } | null>(null)
  const [citePopup, setCitePopup] = useState<{
    marker: string
    rect: DOMRect
    el: HTMLElement
  } | null>(null)
  const [citeItems, setCiteItems] = useState<ProvenanceCitation[] | null>(null)
  const [citeBusy, setCiteBusy] = useState(false)
  const hasCites = b.kind === 'para' && hasCitation(b.text)
  const blockAnnotations = useMemo(
    () => annotations.filter((a) => a.block_id === b.id),
    [annotations, b.id],
  )
  const blockHighlights = useMemo(
    () => highlights.filter((h) => h.block_id === b.id),
    [highlights, b.id],
  )

  const openProvenance = useCallback(async () => {
    setProvOpen(true)
    if (prov || !paperId || provBusy) return
    setProvBusy(true)
    try {
      setProv(await getProvenance(paperId, b.id))
    } catch {
      setProv([])
    } finally {
      setProvBusy(false)
    }
  }, [prov, paperId, provBusy, b.id])

  // 深链 ?prov=b0010 直接打开某段的出处
  useEffect(() => {
    const target = new URLSearchParams(window.location.search).get('prov')
    if (target && target === b.id && b.kind === 'para') void openProvenance()
  }, [b.id, b.kind, openProvenance])

  const openCite = useCallback(
    async (marker: string, rect: DOMRect, el: HTMLElement) => {
      setCitePopup({ marker, rect, el })
      if (prov) {
        setCiteItems(prov)
        return
      }
      if (!paperId) {
        setCiteItems([])
        return
      }
      setCiteBusy(true)
      try {
        const items = await getProvenance(paperId, b.id)
        setProv(items)
        setCiteItems(items)
      } catch {
        setCiteItems([])
      } finally {
        setCiteBusy(false)
      }
    },
    [prov, paperId, b.id],
  )

  const onParagraphMouseUp = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!annotationActions || !paperId) return
    // 点在高亮/引用按钮上时不进入划词流程
    const target = event.target as HTMLElement
    if (target.closest('[data-aid], .cite-mark')) return

    window.setTimeout(() => {
      const sel = window.getSelection()
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) return
      const range = sel.getRangeAt(0)
      const startEl =
        range.startContainer.nodeType === Node.ELEMENT_NODE
          ? (range.startContainer as HTMLElement)
          : range.startContainer.parentElement
      const para = startEl?.closest<HTMLElement>('p[data-text-source]')
      if (!para || !para.contains(range.endContainer)) return
      if (!para.contains(range.startContainer)) return
      const rect = range.getBoundingClientRect()
      if (!rect.width && !rect.height) return
      const full = para.textContent ?? ''
      const start = offsetIn(para, range.startContainer, range.startOffset)
      const end = offsetIn(para, range.endContainer, range.endOffset)
      if (end <= start) return
      setSelection({
        source: (para.dataset.textSource as 'en' | 'zh') ?? 'en',
        start,
        end,
        quote: full.slice(start, end),
        rect,
      })
    }, 0)
  }

  const applyHighlight = async (color: AnnotationColor) => {
    if (!selection || !annotationActions) return
    const current = selection
    setSelection(null)
    window.getSelection()?.removeAllRanges()
    try {
      await annotationActions.create({
        block_id: b.id,
        source: current.source,
        start: current.start,
        end: current.end,
        quote: current.quote,
        color,
      })
    } catch (err) {
      console.warn('添加标注失败：', err)
    }
  }

  const copy = () => {
    navigator.clipboard?.writeText(b.translated || b.text).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }

  if (b.kind === 'heading') {
    const Tag = b.level <= 1 ? 'h2' : b.level === 2 ? 'h3' : 'h4'
    const cls = b.level <= 1 ? 'h h-1' : b.level === 2 ? 'h h-2' : 'h h-3'
    // 与正文一致：英文在上、中文在下
    const showEn = mode !== 'zh' || !b.translated
    const showZh = mode !== 'en' && Boolean(b.translated)
    return (
      <Tag id={b.id} data-heading-id={b.id} className={cls}>
        {showEn && <span className="h-en">{b.text}</span>}
        {showZh && <span className="h-zh">{b.translated}</span>}
      </Tag>
    )
  }

  if (b.kind === 'equation') {
    return (
      <div className="block equation" id={b.id} data-block-id={b.id}>
        <EquationBody
          block={b}
          attached={Boolean(attachedIds?.has(b.id))}
          onAttach={onAttachContext}
        />
      </div>
    )
  }

  if (b.kind === 'figure') {
    return (
      <figure className="block figure" id={b.id} data-block-id={b.id}>
        <div className="float-body">
          {onAttachContext && (
            <AttachButton
              attached={Boolean(attachedIds?.has(b.id))}
              onClick={() =>
                onAttachContext({
                  id: b.id,
                  blockId: b.id,
                  kind: 'figure',
                  label: floatLabel(b),
                  page: b.page,
                  caption: rb.captionEn || b.caption,
                  imageUrl: b.image_path ?? undefined,
                })
              }
            />
          )}
          <div className="figure-frame">
            <img src={b.image_path ?? ''} alt={rb.captionEn ?? ''} loading="lazy" />
          </div>
        </div>
        <Caption zh={rb.captionZh} en={rb.captionEn} />
      </figure>
    )
  }

  // 表格：只当整图切片展示，不做结构提取（数字对齐、合并单元格交给原排版）
  if (b.kind === 'table') {
    return (
      <figure className="block figure" id={b.id} data-block-id={b.id}>
        <div className="float-body">
          {onAttachContext && (
            <AttachButton
              attached={Boolean(attachedIds?.has(b.id))}
              onClick={() =>
                onAttachContext({
                  id: b.id,
                  blockId: b.id,
                  kind: 'table',
                  label: floatLabel(b),
                  page: b.page,
                  caption: rb.captionEn || b.caption,
                  imageUrl: b.image_path ?? undefined,
                })
              }
            />
          )}
          <div className="table-frame">
            {b.image_path ? (
              <img src={b.image_path} alt={rb.captionEn ?? 'table'} loading="lazy" />
            ) : (
              <p style={{ margin: 0, fontSize: 12, color: 'var(--faint)' }}>表格切片缺失</p>
            )}
          </div>
        </div>
        <Caption zh={rb.captionZh} en={rb.captionEn} />
      </figure>
    )
  }

  if (b.kind === 'caption') {
    return (
      <div className="block" id={b.id} data-block-id={b.id}>
        <Caption zh={b.translated} en={b.text} />
      </div>
    )
  }

  if (b.kind === 'keywords') {
    const words = b.text
      .replace(/^keywords?[:：]\s*/i, '')
      .split(/[;；,，]/)
      .map((s) => s.trim())
      .filter(Boolean)
    return (
      <div className="block" id={b.id} data-block-id={b.id}>
        <div className="keywords">
          {words.map((w) => (
            <span key={w}>{w}</span>
          ))}
        </div>
      </div>
    )
  }

  if (b.kind === 'footnote') {
    return (
      <div className="block" id={b.id}>
        <Caption en={b.text} />
      </div>
    )
  }

  // 正文段落：英文原文在上，中文译文在下（术语 + 高亮 + 引用）
  // 没有译文时不显示骨架条——整篇未翻译时否则会全屏转圈
  const showZh = b.translatable && (Boolean(b.translated) || hasTranslation)
  return (
    <div className="block" id={b.id} data-block-id={b.id}>
      <div className="para" onMouseUp={onParagraphMouseUp}>
        <RichText
          text={b.text}
          source="en"
          annotations={blockAnnotations}
          highlights={blockHighlights}
          className="para-en"
          onCite={openCite}
          onAnnotationClick={(annotation, rect) => setEditing({ annotation, rect })}
        />
        {showZh &&
          (b.translated ? (
            <RichText
              text={b.translated}
              source="zh"
              annotations={blockAnnotations}
              highlights={blockHighlights}
              terms={terms}
              className="para-zh"
              onCite={openCite}
              onAnnotationClick={(annotation, rect) => setEditing({ annotation, rect })}
            />
          ) : (
            <p className="para-zh">
              <span className="shimmer" />
            </p>
          ))}
      </div>

      <div className="block-actions">
        <span className="page-chip">P{b.page}</span>
        {hasCites && (
          <button className="icon-btn cite-btn" onClick={openProvenance} title="这段话的出处">
            引
          </button>
        )}
        <button className="icon-btn" onClick={copy} title="复制这一段">
          {copied ? <IconCheck /> : <IconCopy />}
        </button>
      </div>

      {selection && (
        <SelectionToolbar
          rect={selection.rect}
          onPick={(color) => void applyHighlight(color)}
          onClose={() => {
            setSelection(null)
            window.getSelection()?.removeAllRanges()
          }}
        />
      )}

      {editing && annotationActions && (
        <AnnotationPopover
          annotation={editing.annotation}
          rect={editing.rect}
          onSave={async (patch) => {
            await annotationActions.update(editing.annotation.id, patch)
            setEditing(null)
          }}
          onDelete={async () => {
            await annotationActions.remove(editing.annotation.id)
            setEditing(null)
          }}
          onClose={() => setEditing(null)}
        />
      )}

      {citePopup && (
        <CitePopover
          marker={citePopup.marker}
          anchor={citePopup.el}
          initialRect={citePopup.rect}
          loading={citeBusy}
          citations={matchMarkers(citeItems, citePopup.marker)}
          onClose={() => setCitePopup(null)}
        />
      )}

      {provOpen && (
        <ProvenanceDialog
          loading={provBusy}
          citations={prov}
          onClose={() => setProvOpen(false)}
        />
      )}
    </div>
  )
}

/** 选区相对段落文本的字符偏移。 */
function offsetIn(root: HTMLElement, node: Node, offset: number): number {
  const range = document.createRange()
  range.selectNodeContents(root)
  range.setEnd(node, offset)
  return range.toString().length
}

/** [12] 可能对应解析出的 [12]；[12,13] 则命中其中每一条。 */
function matchMarkers(
  items: ProvenanceCitation[] | null,
  marker: string,
): ProvenanceCitation[] {
  if (!items) return []
  const clean = (value: string) => value.replace(/\s+/g, '')
  const target = clean(marker)
  const exact = items.filter((item) => clean(item.marker) === target)
  if (exact.length) return exact
  return items.filter((item) => target.includes(clean(item.marker)))
}

/** 同一时刻只允许一个引用浮层：打开新的会先关掉旧的。 */
let activeCiteCloser: (() => void) | null = null

/** 浮层与视口边缘的最小距离。 */
const CITE_VIEW_GAP = 12
/** 引用锚点随正文滚动超过这个距离就收起浮层。 */
const CITE_SCROLL_CLOSE = 180
/** 引用浮层的最大宽度（手机屏再窄就贴着屏幕两边）。 */
const CITE_WIDTH = 380

function citePosition(rect: DOMRect, height = 180) {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const width = Math.min(CITE_WIDTH, vw - CITE_VIEW_GAP * 2)
  // 手机端顶栏和底部工具条都是固定的，浮层躲开它们，别挡住/被挡住
  const compact = window.matchMedia('(max-width: 860px)').matches
  const topPad = compact ? 84 : CITE_VIEW_GAP
  const bottomPad = compact ? 74 : CITE_VIEW_GAP
  const left = Math.max(
    CITE_VIEW_GAP,
    Math.min(vw - width - CITE_VIEW_GAP, rect.left + rect.width / 2 - width / 2),
  )
  const gap = 8
  const below = rect.bottom + gap
  const above = rect.top - height - gap
  // 优先放在引用下方；下方放不下、上方又更宽敞时翻到上方
  const top = below + height <= vh - bottomPad || above < topPad ? below : above
  return {
    left,
    top: Math.max(topPad, Math.min(vh - height - bottomPad, top)),
    width,
  }
}

/** 点行内引用编号后，只显示这一条（或这一组）引文。 */
function CitePopover({
  marker,
  anchor,
  initialRect,
  loading,
  citations,
  onClose,
}: {
  marker: string
  anchor: HTMLElement
  initialRect: DOMRect
  loading: boolean
  citations: ProvenanceCitation[]
  onClose: () => void
}) {
  const boxRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  // 全局只保留一个：挂载时顺手关掉上一个
  useEffect(() => {
    const close = () => closeRef.current()
    if (activeCiteCloser && activeCiteCloser !== close) activeCiteCloser()
    activeCiteCloser = close
    return () => {
      if (activeCiteCloser === close) activeCiteCloser = null
    }
  }, [])

  // 跟着正文滚动走；滚远或引用离开视口就自动关闭
  useLayoutEffect(() => {
    const startTop = initialRect.top
    let frame = 0
    const place = () => {
      frame = 0
      const box = boxRef.current
      if (!box) return
      if (!anchor.isConnected) {
        closeRef.current()
        return
      }
      const rect = anchor.getBoundingClientRect()
      if (
        rect.bottom < 0 ||
        rect.top > window.innerHeight ||
        Math.abs(rect.top - startTop) > CITE_SCROLL_CLOSE
      ) {
        closeRef.current()
        return
      }
      const next = citePosition(rect, box.offsetHeight || 180)
      box.style.left = `${next.left}px`
      box.style.top = `${next.top}px`
      box.style.width = `${next.width}px`
    }
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(place)
    }

    place()
    window.addEventListener('scroll', schedule, true)
    window.addEventListener('resize', schedule)
    // 内容从「加载中」换成结果后高度会变，重新摆一次
    const observer = new ResizeObserver(schedule)
    if (boxRef.current) observer.observe(boxRef.current)
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      window.removeEventListener('scroll', schedule, true)
      window.removeEventListener('resize', schedule)
      observer.disconnect()
    }
  }, [anchor, initialRect.top])

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement
      if (target.closest('.cite-popover') || target.closest('.cite-mark')) return
      onClose()
    }
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    document.addEventListener('pointerdown', onPointerDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  return (
    <div className="cite-popover" ref={boxRef} style={citePosition(initialRect)}>
      <header className="cite-popover-head">
        <span className="prov-marker">{marker}</span>
        <button className="icon-btn" onClick={onClose} title="关闭">
          ×
        </button>
      </header>
      {loading ? (
        <div className="prov-empty">
          <div className="spinner" />
          正在找回这条引用…
        </div>
      ) : citations.length === 0 ? (
        <div className="prov-empty">没有解析到这条引用，可以在「引」面板里看整段出处。</div>
      ) : (
        citations.map((c, i) => (
          <div className="prov-item" key={`${c.ref.index}-${i}`}>
            <div className="prov-title">
              <span className="prov-marker">{c.marker}</span>
              <b>{c.ref.title}</b>
              {c.ref.year && <span className="prov-year">{c.ref.year}</span>}
            </div>
            {c.snippet ? (
              <p className="prov-snippet">“{c.snippet}”</p>
            ) : (
              <p className="prov-snippet muted">没有取到原文片段（可能是付费墙或未收录）</p>
            )}
            <div className="prov-foot">
              {c.url && (
                <a href={c.url} target="_blank" rel="noreferrer">
                  打开原文 ↗
                </a>
              )}
              <span className="prov-source">{c.source}</span>
            </div>
          </div>
        ))
      )}
    </div>
  )
}

function ProvenanceDialog({
  loading,
  citations,
  onClose,
}: {
  loading: boolean
  citations: ProvenanceCitation[] | null
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-mask" onMouseDown={onClose}>
      <div className="prov-modal" onMouseDown={(e) => e.stopPropagation()}>
        <header className="prov-head">
          <div>
            <h3>这段话的出处</h3>
            <p>引用来自本文参考文献；片段取自原文摘要，未改写。</p>
          </div>
          <button className="icon-btn" onClick={onClose} title="关闭">
            <IconClose />
          </button>
        </header>

        <div className="prov-body">
          {loading && (
            <div className="prov-empty">
              <div className="spinner" />
              正在找回原文…
            </div>
          )}

          {!loading && citations && citations.length === 0 && (
            <div className="prov-empty">这一段没有可解析的引用。</div>
          )}

          {!loading &&
            citations?.map((c, i) => (
              <div className="prov-item" key={`${c.ref.index}-${i}`}>
                <div className="prov-title">
                  <span className="prov-marker">{c.marker}</span>
                  <b>{c.ref.title}</b>
                  {c.ref.year && <span className="prov-year">{c.ref.year}</span>}
                </div>
                {c.snippet ? (
                  <p className="prov-snippet">“{c.snippet}”</p>
                ) : (
                  <p className="prov-snippet muted">没有取到原文片段（可能是付费墙或未收录）</p>
                )}
                <div className="prov-foot">
                  {c.url && (
                    <a href={c.url} target="_blank" rel="noreferrer">
                      打开原文 ↗
                    </a>
                  )}
                  <span className="prov-source">{c.source}</span>
                </div>
              </div>
            ))}
        </div>
      </div>
    </div>
  )
}

/** 图/表编号：Figure 3 / TABLE II / Table S1。 */
const FLOAT_LABEL_RE =
  /^(?:Supplementary\s+)?(?:Figure|Fig\.|FIGURE|Table|TABLE|Tab\.)\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z]\d+)/i

function floatLabel(b: Block): string {
  const m = (b.caption || '').trim().match(FLOAT_LABEL_RE)
  if (m) return m[0].replace(/\s+/g, ' ')
  return b.kind === 'table' ? `表 P${b.page}` : `图 P${b.page}`
}

function AttachButton({ attached, onClick }: { attached: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      className="block-attach"
      data-on={attached}
      title={attached ? '已在问答附录中' : '加入问答附录'}
      aria-label={attached ? '已在问答附录中' : '加入问答附录'}
      onClick={(event) => {
        event.stopPropagation()
        onClick()
      }}
    >
      {attached ? <IconCheck /> : <IconPlus />}
    </button>
  )
}

/** 一次会话内缓存识别结果：滚动来回不重复请求视觉模型。 */
const formulaCache = new Map<string, string>()
const formulaTried = new Set<string>()

/** 公式：优先 KaTeX 矢量渲染并保留编号，识别失败/不支持时回落到裁图。 */
function EquationBody({
  block,
  attached,
  onAttach,
}: {
  block: Block
  attached: boolean
  onAttach?: (item: ReaderContextItem) => void
}) {
  const [tex, setTex] = useState(block.latex ?? '')
  const [imgMode, setImgMode] = useState(!block.latex)
  const hostRef = useRef<HTMLDivElement>(null)
  const fallback = useCallback(() => setImgMode(true), [])

  useEffect(() => {
    if (block.latex && block.latex !== tex) {
      setTex(block.latex)
      setImgMode(false)
    }
    // tex 由识别流程写入，不应因为 tex 变化反复触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [block.latex])

  // 旧论文解析时没有配识图模型：公式进入视野后按需识别一次，
  // 成功后仍然用 KaTeX 渲染，而不是把截图直接交给用户。
  useEffect(() => {
    if (tex || !block.image_path) return
    const cached = formulaCache.get(block.id)
    if (cached) {
      setTex(cached)
      setImgMode(false)
      return
    }
    const cfg = visionModel()
    if (!cfg || formulaTried.has(block.id)) return
    const el = hostRef.current
    if (!el) return

    let alive = true
    const recognize = async () => {
      if (formulaTried.has(block.id)) return
      formulaTried.add(block.id)
      try {
        const res = await fetch(block.image_path as string)
        const blob = await res.blob()
        const b64 = await new Promise<string>((resolve) => {
          const reader = new FileReader()
          reader.onload = () => {
            const data = String(reader.result ?? '')
            resolve(data.includes(',') ? data.split(',')[1] : '')
          }
          reader.readAsDataURL(blob)
        })
        if (!b64) return
        const resp = await fetch('/api/formula', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ image_base64: b64, config: configBody(cfg) }),
        })
        if (!resp.ok) return
        const data = (await resp.json()) as { latex?: string }
        if (!alive || !data.latex) return
        formulaCache.set(block.id, data.latex)
        setTex(data.latex)
        setImgMode(false)
      } catch {
        formulaTried.delete(block.id) // 网络失败：下次进入视野还能再试
      }
    }

    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          void recognize()
          io.disconnect()
        }
      },
      { rootMargin: '240px' },
    )
    io.observe(el)
    return () => {
      alive = false
      io.disconnect()
    }
  }, [block.id, block.image_path, tex])

  const label = block.eq_number ? `公式 (${block.eq_number})` : '公式'
  const attach = onAttach ? (
    <AttachButton
      attached={attached}
      onClick={() =>
        onAttach({
          id: block.id,
          blockId: block.id,
          kind: 'equation',
          label,
          page: block.page,
          latex: tex || block.latex,
          caption: block.text.slice(0, 160),
        })
      }
    />
  ) : null

  if (!imgMode && tex) {
    return (
      <div className="equation-body" ref={hostRef}>
        <div className="equation-row">
          <Katex tex={tex} onError={fallback} />
          {block.eq_number && <span className="eq-number">({block.eq_number})</span>}
        </div>
        <button
          className="eq-copy"
          title="复制 LaTeX"
          onClick={() => navigator.clipboard?.writeText(tex)}
        >
          TeX
        </button>
        {attach}
      </div>
    )
  }

  // 裁图分辨率变了，但显示宽度要按 PDF 里的实际宽度算（约 2.8px/pt，
  // 与原来的 200dpi 观感一致），否则窄公式会被放大成巨幅。
  const bbox = block.bbox
  const pts = bbox && bbox.length >= 4 ? bbox[2] - bbox[0] : 0
  const width = Number.isFinite(pts) && pts > 0 ? Math.round(pts * 2.8) : 0

  return (
    <div className="equation-body" ref={hostRef}>
      {block.image_path ? (
        <div className="equation-row is-image">
          <img
            src={block.image_path}
            alt="equation"
            loading="lazy"
            style={width ? { width: `min(100%, ${width}px)` } : undefined}
          />
          {block.eq_number && <span className="eq-number">({block.eq_number})</span>}
        </div>
      ) : (
        <pre>{block.text}</pre>
      )}
      {attach}
    </div>
  )
}

function Caption({ zh, en }: { zh?: string; en?: string }) {
  if (!zh && !en) return null
  // 标签单独渲染，正文里再出现的 "Table 1:" 要去掉，否则会出现 "Table 1 Table 1: ..."
  const label = (en ?? '').trim().match(FLOAT_LABEL_RE)?.[0]
  const prefix = label
    ? new RegExp(`^${label.split(/\s+/).join('\\s*')}\\s*[:：.]?\\s*`, 'i')
    : null
  // 中文译文可能把编号译成「图 1：」「表 II：」，也一并去掉
  const CN_PREFIX_RE =
    /^(?:Supplementary\s+)?(?:Figure|Fig\.?|Table|Tab\.?|图|表)\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z]\d+)\s*[:：.]?\s*/i
  const strip = (s: string) =>
    (prefix ? s.replace(prefix, '') : s).replace(CN_PREFIX_RE, '')
  return (
    <figcaption className="caption">
      {zh ? (
        <span>
          {label && <b className="cap-label">{label}</b>}
          {strip(zh)}
        </span>
      ) : (
        <span className="cap-en">{strip(en ?? '')}</span>
      )}
      {zh && <span className="cap-en">{strip(en ?? '')}</span>}
    </figcaption>
  )
}
