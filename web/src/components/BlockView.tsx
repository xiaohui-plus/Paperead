import { useCallback, useEffect, useState } from 'react'
import type { Block, ViewMode } from '../types'
import type { RenderBlock } from '../lib/blocks'
import { getProvenance, type ProvenanceCitation } from '../lib/library'
import { TermText } from '../lib/highlight'
import { Katex } from './Katex'
import { IconCheck, IconClose, IconCopy } from './icons'

interface Props {
  rb: RenderBlock
  mode: ViewMode
  terms: Record<string, string>
  /** 整篇是否有任何译文：没有的话不显示骨架条，避免整页都在转圈 */
  hasTranslation: boolean
  paperId?: string
}

// 数字引用 [12] / [3-5]，或作者-年份 (Zhang et al. 2018)
const CITE_RE = /\[\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*\]|\([A-Z][A-Za-z\-']+(?:\s+(?:et\s+al\.?|and\s+[A-Z][A-Za-z\-']+))?\s*,?\s*(?:19|20)\d{2}[a-z]?\)/

export function BlockView({ rb, mode, terms, hasTranslation, paperId }: Props) {
  const b = rb.block
  const [copied, setCopied] = useState(false)
  const [provOpen, setProvOpen] = useState(false)
  const [prov, setProv] = useState<ProvenanceCitation[] | null>(null)
  const [provBusy, setProvBusy] = useState(false)
  const hasCites = b.kind === 'para' && CITE_RE.test(b.text)

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
        <EquationBody block={b} />
      </div>
    )
  }

  if (b.kind === 'figure') {
    return (
      <figure className="block figure" id={b.id} data-block-id={b.id}>
        <div className="figure-frame">
          <img src={b.image_path ?? ''} alt={rb.captionEn ?? ''} loading="lazy" />
        </div>
        <Caption zh={rb.captionZh} en={rb.captionEn} />
      </figure>
    )
  }

  // 表格：只当整图切片展示，不做结构提取（数字对齐、合并单元格交给原排版）
  if (b.kind === 'table') {
    return (
      <figure className="block figure" id={b.id} data-block-id={b.id}>
        <div className="table-frame">
          {b.image_path ? (
            <img src={b.image_path} alt={rb.captionEn ?? 'table'} loading="lazy" />
          ) : (
            <p style={{ margin: 0, fontSize: 12, color: 'var(--faint)' }}>表格切片缺失</p>
          )}
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

  // 正文段落：英文原文在上，中文译文在下（术语高亮）
  // 没有译文时不显示骨架条——整篇未翻译时否则会全屏转圈
  const showZh = b.translatable && (Boolean(b.translated) || hasTranslation)
  return (
    <div className="block" id={b.id} data-block-id={b.id}>
      <div className="para">
        <p className="para-en">{b.text}</p>
        {showZh && (
          <p className="para-zh">
            {b.translated ? (
              <TermText text={b.translated} terms={terms} />
            ) : (
              <span className="shimmer" />
            )}
          </p>
        )}
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

/** 公式：优先 KaTeX 矢量渲染，识别失败/不支持时回落到裁图。 */
function EquationBody({ block }: { block: Block }) {
  const [imgMode, setImgMode] = useState(!block.latex)
  const fallback = useCallback(() => setImgMode(true), [])

  if (!imgMode && block.latex) {
    return (
      <div style={{ position: 'relative' }}>
        <Katex tex={block.latex} onError={fallback} />
        <button
          className="eq-copy"
          title="复制 LaTeX"
          onClick={() => navigator.clipboard?.writeText(block.latex ?? '')}
        >
          TeX
        </button>
      </div>
    )
  }

  return block.image_path ? (
    <img src={block.image_path} alt="equation" loading="lazy" />
  ) : (
    <pre>{block.text}</pre>
  )
}

function Caption({ zh, en }: { zh?: string; en?: string }) {
  if (!zh && !en) return null
  // 标签单独渲染，正文里再出现的 "Table 1:" 要去掉，否则会出现 "Table 1 Table 1: ..."
  const strip = (s: string) => s.replace(/^(Figure|Table|Fig\.|Tab\.)\s*\d+\s*[:：]\s*/i, '')
  const label = (en ?? '').match(/^(Figure|Table|Fig\.|Tab\.)\s*\d+/i)?.[0]
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
