import type { KeyPoint } from '../lib/assistant'
import type { HighlightCategory, ReadingHighlight } from '../lib/library'
import { HIGHLIGHT_LABELS } from './Annotations'

interface Props {
  highlights: ReadingHighlight[]
  points: KeyPoint[]
  onJump: (id: string) => void
}

const ORDER: HighlightCategory[] = ['objective', 'method', 'conclusion']

/**
 * 阅读重点：正文翻译完成后自动抽出的研究目标 / 方法 / 结论，
 * 按类别分组，原文和译文并排摘录；点击回到原文位置。
 */
export function KeyPointsPanel({ highlights, points, onJump }: Props) {
  if (!highlights.length) {
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
      <p className="quick-label kp-intro">
        翻译完成一页就自动标一页；正文里的颜色和这里一一对应。
      </p>
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
                className={`kp-card auto-${category}`}
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
    </div>
  )
}
