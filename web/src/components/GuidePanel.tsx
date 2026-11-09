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
  return `paperead.mastered.${paperId}`
}

const KIND_LABEL: Record<string, string> = {
  term: '术语',
  method: '方法',
  dataset: '数据集',
  metric: '指标',
  abbr: '缩写',
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
          正在读懂这篇论文：概括内容 → 提取术语 → 梳理结构…
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
  const ov = guide.overview || {}
  const glossary = guide.glossary || []
  const outline = guide.outline || []
  const questions = guide.questions || []
  const prereqs = guide.prereqs || []
  const pathSteps = guide.path || []
  const hasOverview = Boolean(
    ov.problem ||
      ov.approach ||
      (ov.contributions && ov.contributions.length) ||
      (ov.findings && ov.findings.length) ||
      (ov.limitations && ov.limitations.length),
  )

  return (
    <div className="guide">
      <section className="guide-summary">
        <p className="guide-eyebrow">循序渐进导读</p>
        {guide.oneliner && <p className="guide-oneliner">{guide.oneliner}</p>}
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
          {glossary.length > 0 && <span>术语 · {glossary.length} 个</span>}
        </div>
      </section>

      {hasOverview && (
        <section className="guide-block">
          <h4>论文主要内容</h4>
          <div className="guide-overview">
            {ov.problem && (
              <div className="ov-row">
                <span className="ov-label">要解决的问题</span>
                <p>{ov.problem}</p>
              </div>
            )}
            {ov.approach && (
              <div className="ov-row">
                <span className="ov-label">做法</span>
                <p>{ov.approach}</p>
              </div>
            )}
            {(ov.contributions || []).length > 0 && (
              <div className="ov-row">
                <span className="ov-label">主要贡献</span>
                <ul className="ov-list">
                  {(ov.contributions || []).map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </div>
            )}
            {(ov.findings || []).length > 0 && (
              <div className="ov-row">
                <span className="ov-label">主要发现</span>
                <ul className="ov-list">
                  {(ov.findings || []).map((f, i) => (
                    <li key={i}>{f}</li>
                  ))}
                </ul>
              </div>
            )}
            {(ov.limitations || []).length > 0 && (
              <div className="ov-row">
                <span className="ov-label">局限</span>
                <ul className="ov-list">
                  {(ov.limitations || []).map((l, i) => (
                    <li key={i}>{l}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </section>
      )}

      {glossary.length > 0 && (
        <section className="guide-block">
          <h4>
            专业词语对照表
            <span className="guide-count">{glossary.length}</span>
          </h4>
          <div className="term-table">
            {glossary.map((t) => (
              <div className="term-row" key={t.en}>
                <div className="term-main">
                  <span className="term-en">{t.en}</span>
                  {t.zh && t.zh !== t.en && <span className="term-zh">{t.zh}</span>}
                  <span className="term-kind">{KIND_LABEL[t.kind] || t.kind}</span>
                </div>
                {t.explain && <p className="term-explain">{t.explain}</p>}
                {t.where && <p className="term-where">出现于 {t.where}</p>}
              </div>
            ))}
          </div>
        </section>
      )}

      {outline.length > 0 && (
        <section className="guide-block">
          <h4>论文结构地图</h4>
          <ol className="outline-list">
            {outline.map((o, i) => (
              <li key={`${o.section}-${i}`}>
                <div className="outline-head">
                  <b>{o.section}</b>
                  <span className="outline-idx">{String(i + 1).padStart(2, '0')}</span>
                </div>
                {o.what && <p className="outline-what">{o.what}</p>}
                {o.focus && <p className="outline-focus">阅读时关注：{o.focus}</p>}
              </li>
            ))}
          </ol>
        </section>
      )}

      {prereqs.length > 0 && (
        <section className="guide-block">
          <h4>看不懂时，先补这些</h4>
          {prereqs.map((p) => {
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

      {pathSteps.length > 0 && (
        <section className="guide-block">
          <h4>推荐阅读顺序</h4>
          <p className="path-intro">
            按「先补概念 → 再看方法来源 → 最后读本文」排列。每一步都说明为什么值得读、为什么排在这里。
          </p>
          <ol className="path-list">
            {pathSteps.map((s, i) => {
              const isSelf = s.kind === 'paper'
              return (
                <li key={`${s.title}-${i}`} className={isSelf ? 'is-self' : ''}>
                  <div className="path-step-head">
                    <b>
                      <span className="path-idx">{String(i + 1).padStart(2, '0')}</span>
                      {s.title}
                    </b>
                    <span className="path-min">
                      {s.year ? `${s.year} · ` : ''}
                      {s.minutes} min
                    </span>
                  </div>
                  {!isSelf && s.concept && (
                    <span className="path-tag">为「{s.concept}」打底</span>
                  )}
                  {s.why && (
                    <p className="path-why">
                      <em>为什么读</em>
                      {s.why}
                    </p>
                  )}
                  {s.order && (
                    <p className="path-order">
                      <em>为什么排这里</em>
                      {s.order}
                    </p>
                  )}
                  {s.gain && (
                    <p className="path-gain">
                      <em>读完收获</em>
                      {s.gain}
                    </p>
                  )}
                </li>
              )
            })}
          </ol>
        </section>
      )}

      {questions.length > 0 && (
        <section className="guide-block">
          <h4>读完自测</h4>
          <ul className="selfcheck-list">
            {questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
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
