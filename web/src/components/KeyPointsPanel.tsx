import { useEffect, useMemo, useState } from 'react'
import type { KeyPoint } from '../lib/assistant'
import type { Annotation, HighlightCategory, ReadingHighlight } from '../lib/library'
import { HIGHLIGHT_LABELS } from './Annotations'

interface Props {
  highlights: ReadingHighlight[]
  points: KeyPoint[]
  annotations?: Annotation[]
  onJump: (id: string) => void
  /** 双击正文里的自动阅读重点后，滚动并高亮这一条 */
  focusHighlight?: { id: string; at: number } | null
}

const ORDER: HighlightCategory[] = ['objective', 'method', 'conclusion']

/**
 * 阅读重点：正文翻译完成后自动抽出的研究目标 / 方法 / 结论，按类别分组；
 * 用户自己的荧光笔高亮也收在这里的「我的荧光笔」分组（批注栏里同样可见）。
 */
export function KeyPointsPanel({
  highlights,
  points,
  annotations = [],
  onJump,
  focusHighlight,
}: Props) {
  const [flashId, setFlashId] = useState('')

  useEffect(() => {
    if (!focusHighlight) return
    setFlashId(focusHighlight.id)
    const el = document.getElementById(`kp-${focusHighlight.id}`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    const timer = window.setTimeout(() => setFlashId(''), 2200)
    return () => window.clearTimeout(timer)
  }, [focusHighlight])

  const mine = useMemo(
    () => annotations.filter((item) => item.kind === 'highlight'),
    [annotations],
  )

  if (!highlights.length && !mine.length) {
    return (
      <div className="kp-empty">
        <p>读完当前页并完成翻译后，这里会自动按「研究目标 / 研究方法 / 主要结论」摘录重点。</p>
        <p className="kp-empty-hint">
          如果关闭了「设置 → 自动阅读 → 自动标记阅读重点」，这里不会自动生成，下面先按章节首句速览：
        </p>
        {points.length ? (
          points.map((p) => (
            <button key={p.id} className="kp-item kp-fallback" onClick={() => onJump(p.id)}>
              <h4>{p.title}</h4>
              <p>{p.text}</p>
            </button>
          ))
        ) : (
          <p className="kp-empty-hint">当前页没有识别到可提取的重点。</p>
        )}
      </div>
    )
  }

  return (
    <div className="kp-panel">
      {highlights.length > 0 && (
        <p className="quick-label kp-intro">
          翻译完成一页就自动标一页；正文里的颜色和这里一一对应。
        </p>
      )}
      {ORDER.map((category) => {
        const items = highlights
          .filter((item) => item.category === category)
          .sort((a, b) => a.page - b.page || a.block_id.localeCompare(b.block_id))
        if (!items.length) return null
        return (
          <section className="kp-group" key={category}>
            <div className="kp-group-head">
              <span className={`kp-swatch auto-${category}`} aria-hidden />
              <h3>{HIGHLIGHT_LABELS[category]}</h3>
              <span className="kp-count">{items.length}</span>
            </div>
            {items.map((item) => (
              <button
                key={item.id}
                id={`kp-${item.id}`}
                data-highlight-id={item.id}
                className={`kp-card auto-${category}${flashId === item.id ? ' is-focus' : ''}`}
                onClick={() => onJump(item.block_id)}
                title="跳到原文"
              >
                <div className="kp-card-meta">
                  <span>P{item.page}</span>
                  <span>{HIGHLIGHT_LABELS[item.category]}</span>
                </div>
                <p className="kp-card-en">{item.quote_en}</p>
                <p className="kp-card-zh">{item.quote_zh}</p>
              </button>
            ))}
          </section>
        )
      })}

      {mine.length > 0 && (
        <section className="kp-group">
          <div className="kp-group-head">
            <span className="kp-swatch kp-swatch-user" aria-hidden />
            <h3>我的荧光笔</h3>
            <span className="kp-count">{mine.length}</span>
          </div>
          {[...mine]
            .sort((a, b) => a.block_id.localeCompare(b.block_id) || a.start - b.start)
            .map((item) => {
              const en =
                item.source === 'en'
                  ? item.quote
                  : item.pair?.source === 'en'
                    ? item.pair.quote
                    : ''
              const zh =
                item.source === 'zh'
                  ? item.quote
                  : item.pair?.source === 'zh'
                    ? item.pair.quote
                    : ''
              return (
                <button
                  key={`${item.id}-${item.updated_at}`}
                  className="kp-card kp-card-user"
                  onClick={() => onJump(item.block_id)}
                  title="跳到原文"
                >
                  <div className="kp-card-meta">
                    <span className={`kp-swatch cm-${item.color}`} aria-hidden />
                    <span>{item.source === 'en' ? '原文' : '译文'}</span>
                  </div>
                  {en && <p className="kp-card-en">{en}</p>}
                  {zh && <p className="kp-card-zh">{zh}</p>}
                  {item.note.trim() && <p className="kp-card-zh">{item.note}</p>}
                </button>
              )
            })}
        </section>
      )}
    </div>
  )
}
