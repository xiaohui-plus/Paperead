import { useCallback, useEffect, useState } from 'react'
import { getGuide, makeGuide, type Guide } from '../lib/library'
import { loadModelSettings, readyModel, toPayload } from '../lib/settings'
import { IconSpark } from './icons'

interface Props {
  paperId: string
  onOpenSettings: () => void
  settingsVersion?: number
}

function masteredKey(paperId: string) {
  return `paper-reflow.mastered.${paperId}`
}

export function GuidePanel({ paperId, onOpenSettings, settingsVersion = 0 }: Props) {
  const [guide, setGuide] = useState<Guide | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [needsConfig, setNeedsConfig] = useState(false)
  const [mastered, setMastered] = useState<Set<string>>(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(masteredKey(paperId)) || '[]'))
    } catch {
      return new Set()
    }
  })

  const generate = useCallback(
    async (refresh = false) => {
      const cfg = readyModel(loadModelSettings(), 'answer')
      if (!cfg) {
        setNeedsConfig(true)
        setLoading(false)
        return
      }
      setLoading(true)
      setError('')
      setNeedsConfig(false)
      try {
        const g = await makeGuide(paperId, toPayload(cfg), refresh)
        setGuide(g)
      } catch (e) {
        setError(e instanceof Error ? e.message : '导读生成失败')
      } finally {
        setLoading(false)
      }
    },
    [paperId],
  )

  useEffect(() => {
    let alive = true
    ;(async () => {
      if (paperId === 'demo') {
        setLoading(false)
        return
      }
      try {
        const cached = await getGuide(paperId)
        if (!alive) return
        if (cached) {
          setGuide(cached)
          setLoading(false)
        } else {
          await generate(false)
        }
      } catch {
        if (alive) await generate(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [paperId, generate])

  // 之前因为没配置模型停在「去设置」时，保存配置后自动继续生成
  useEffect(() => {
    if (!needsConfig) return
    if (!readyModel(loadModelSettings(), 'answer')) return
    void generate(false)
  }, [settingsVersion, needsConfig, generate])

  const toggleMastered = (concept: string) => {
    setMastered((prev) => {
      const next = new Set(prev)
      if (next.has(concept)) next.delete(concept)
      else next.add(concept)
      localStorage.setItem(masteredKey(paperId), JSON.stringify([...next]))
      return next
    })
  }

  if (loading) {
    return (
      <div className="guide">
        <div className="guide-loading">
          <div className="spinner" />
          正在梳理这篇论文：抽概念 → 找前置 → 排顺序…
        </div>
      </div>
    )
  }

  if (needsConfig) {
    return (
      <div className="guide">
        <div className="guide-empty">
          <IconSpark />
          <b>导读需要模型</b>
          <span>生成导读要调用一次大模型（约 5 秒）。配置后自动生成。</span>
          <button className="btn primary" onClick={onOpenSettings}>
            去设置
          </button>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="guide">
        <div className="guide-empty">
          <b>生成失败</b>
          <span>{error}</span>
          <button className="btn" onClick={() => generate(true)}>
            重试
          </button>
        </div>
      </div>
    )
  }

  if (!guide) {
    return <div className="guide" />
  }

  const diff = guide.difficulty || {}

  return (
    <div className="guide">
      <section className="guide-summary">
        <p className="guide-eyebrow">3 分钟导读</p>
        <p className="guide-text">{guide.summary}</p>
        <div className="guide-meta">
          {(guide.domains || []).map((d) => (
            <span className="chip-static" key={d}>
              {d}
            </span>
          ))}
        </div>
        <div className="guide-stats">
          <span>难度 {'★'.repeat(diff.stars || 3)}{'☆'.repeat(Math.max(0, 5 - (diff.stars || 3)))}</span>
          <span>预计 {diff.minutes || 30} 分钟</span>
          {diff.math && <span>数学 · {diff.math}</span>}
        </div>
      </section>

      {guide.prereqs.length > 0 && (
        <section className="guide-block">
          <h4>你需要的前置知识</h4>
          {guide.prereqs.map((p) => {
            const done = mastered.has(p.concept)
            return (
              <div className={`prereq ${done ? 'is-done' : ''}`} key={p.concept}>
                <div className="prereq-head">
                  <b>{p.concept}</b>
                  <button
                    className="mini-btn"
                    onClick={() => toggleMastered(p.concept)}
                    title="已掌握就不再提示"
                  >
                    {done ? '已掌握' : '标记已掌握'}
                  </button>
                </div>
                <p className="prereq-why">{p.why}</p>
                <p className="prereq-ref">
                  {p.paper?.year ? `${p.paper.year} · ` : ''}
                  {p.paper?.title}
                </p>
              </div>
            )
          })}
        </section>
      )}

      {guide.path.length > 0 && (
        <section className="guide-block">
          <h4>推荐阅读顺序</h4>
          <ol className="path-list">
            {guide.path.map((s, i) => (
              <li key={`${s.title}-${i}`} className={s.kind === 'paper' ? 'is-self' : ''}>
                <div>
                  <b>{s.title}</b>
                  <span className="path-min">{s.minutes} min</span>
                </div>
                {s.gain && <p>{s.gain}</p>}
              </li>
            ))}
          </ol>
        </section>
      )}

      <div className="guide-foot">
        <button className="mini-btn" onClick={() => generate(true)}>
          重新生成
        </button>
        {guide.degraded && <span className="guide-degraded">模型未返回结构化结果，已降级</span>}
      </div>
    </div>
  )
}
