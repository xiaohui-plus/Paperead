import { useCallback, useEffect, useState } from 'react'
import { getLineage, makeLineage, type Lineage, type LineageNode } from '../lib/library'
import { isConfigured, loadSettings } from '../lib/settings'
import { IconSpark } from './icons'

interface Props {
  paperId: string
  onOpenSettings: () => void
}

const ROLE_CLASS: Record<string, string> = {
  foundation: 'r-foundation',
  inspiration: 'r-inspiration',
  contrast: 'r-contrast',
  tool: 'r-tool',
}

export function LineagePanel({ paperId, onOpenSettings }: Props) {
  const [data, setData] = useState<Lineage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [needsConfig, setNeedsConfig] = useState(false)
  const [open, setOpen] = useState<number | null>(null)

  const generate = useCallback(
    async (refresh = false) => {
      const cfg = loadSettings()
      if (!isConfigured(cfg)) {
        setNeedsConfig(true)
        setLoading(false)
        return
      }
      setLoading(true)
      setError('')
      setNeedsConfig(false)
      try {
        setData(
          await makeLineage(
            paperId,
            { base_url: cfg.baseUrl, api_key: cfg.apiKey, model: cfg.model },
            refresh,
          ),
        )
      } catch (e) {
        setError(e instanceof Error ? e.message : '生成失败')
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
        const cached = await getLineage(paperId)
        if (!alive) return
        if (cached) {
          setData(cached)
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

  if (loading) {
    return (
      <div className="guide">
        <div className="guide-loading">
          <div className="spinner" />
          正在梳理：找出本文从哪些论文获得了启发…
        </div>
      </div>
    )
  }

  if (needsConfig) {
    return (
      <div className="guide">
        <div className="guide-empty">
          <IconSpark />
          <b>脉络需要模型</b>
          <span>生成脉络要调用一次大模型（约 6 秒），用来判断每篇文献的作用。</span>
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

  if (!data || data.nodes.length === 0) {
    return (
      <div className="guide">
        <div className="guide-empty">
          <b>没有找到明确的启发关系</b>
          <span>这篇论文的引用多是罗列式的，无法判断每篇的具体作用。</span>
          <button className="btn" onClick={() => generate(true)}>
            重新分析
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="lineage">
      <p className="lineage-hint">按年份从旧到新。点任意一条看它给了本文什么、本文改了什么。</p>

      <div className="lineage-rail">
        {data.nodes.map((n: LineageNode) => (
          <div className={`ln-item ${ROLE_CLASS[n.role] ?? ''}`} key={n.id}>
            <div className="ln-year">{n.year ?? '—'}</div>
            <div className="ln-body">
              <button className="ln-head" onClick={() => setOpen(open === n.id ? null : n.id)}>
                <span className="ln-dot" />
                <span className="ln-title">{n.title}</span>
                <span className="ln-role">{n.roleLabel}</span>
              </button>
              <p className="ln-borrow">{n.borrow}</p>

              {open === n.id && (
                <div className="ln-detail">
                  {n.improve && (
                    <p>
                      <b>本文改了什么</b>
                      {n.improve}
                    </p>
                  )}
                  {n.evidence && (
                    <p className="ln-evidence">
                      <b>原文依据</b>
                      “{n.evidence}”
                    </p>
                  )}
                  <p className="ln-meta">被本文引用 {n.cites} 次 · {n.marker}</p>
                </div>
              )}
            </div>
          </div>
        ))}

        <div className="ln-item is-self">
          <div className="ln-year">{data.self.year ?? '—'}</div>
          <div className="ln-body">
            <div className="ln-head as-self">
              <span className="ln-dot" />
              <span className="ln-title">本文</span>
            </div>
            <p className="ln-borrow">{data.self.title}</p>
          </div>
        </div>
      </div>

      <div className="guide-foot">
        <button className="mini-btn" onClick={() => generate(true)}>
          重新生成
        </button>
      </div>
    </div>
  )
}
