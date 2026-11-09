import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getLineage,
  makeLineage,
  waitForGenerated,
  type Lineage,
  type LineageNode,
} from '../lib/library'
import { loadModelSettings, readyModel, toPayload } from '../lib/settings'
import { IconSpark } from './icons'

interface Props {
  paperId: string
  onOpenSettings: () => void
  settingsVersion?: number
}

const ROLE_CLASS: Record<string, string> = {
  foundation: 'r-foundation',
  inspiration: 'r-inspiration',
  contrast: 'r-contrast',
  tool: 'r-tool',
}

export function LineagePanel({ paperId, onOpenSettings, settingsVersion = 0 }: Props) {
  const [data, setData] = useState<Lineage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [needsConfig, setNeedsConfig] = useState(false)
  const [open, setOpen] = useState<number | null>(null)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

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
        setData(await makeLineage(paperId, toPayload(cfg), refresh))
      } catch (e) {
        const msg = e instanceof Error ? e.message : '生成失败'
        // 切走再切回来会重复请求，后端以「正在生成中」拒绝；继续等上一次的结果
        if (msg.includes('正在生成中')) {
          const cached = await waitForGenerated(() => getLineage(paperId), () => mountedRef.current)
          if (!mountedRef.current) return
          if (cached) {
            setData(cached)
          } else {
            setError('这次生成耗时较长，稍后重新进入「脉络」即可查看')
          }
          return
        }
        setError(msg)
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

  // 之前因为没配置模型停在「去设置」时，保存配置后自动继续生成
  useEffect(() => {
    if (!needsConfig) return
    if (!readyModel(loadModelSettings(), 'answer')) return
    void generate(false)
  }, [settingsVersion, needsConfig, generate])

  if (loading) {
    return (
      <div className="guide">
        <div className="guide-loading">
          <div className="spinner" />
          正在梳理：结合引用位置与文献标题推断思路来源…
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
    const parsed = data?.stats
    return (
      <div className="guide">
        <div className="guide-empty">
          <b>没有找到明确的启发关系</b>
          <span>
            {parsed
              ? `已解析 ${parsed.references} 篇参考文献、${parsed.uniqueCitations} 个引用，但没有找到足以判断关系的线索。`
              : '这篇论文的引用多是罗列式的，无法判断每篇的具体作用。'}
          </span>
          <button className="btn" onClick={() => generate(true)}>
            重新分析
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="lineage">
      <p className="lineage-hint">
        作者通常不会直说借鉴关系。这里结合引用位置、文献标题与上下文推断；标注「推断」的条目可信度较低，点开可看原文依据。
      </p>

      <div className="lineage-rail">
        {data.nodes.map((n: LineageNode) => (
          <div className={`ln-item ${ROLE_CLASS[n.role] ?? ''}`} key={n.id}>
            <div className="ln-year">{n.year ?? '—'}</div>
            <div className="ln-body">
              <button className="ln-head" onClick={() => setOpen(open === n.id ? null : n.id)}>
                <span className="ln-dot" />
                <span className="ln-title">{n.title}</span>
                <span className="ln-role">{n.roleLabel}</span>
                {n.confidence && n.confidence !== 'high' && (
                  <span className={`ln-conf ${n.confidence}`}>
                    {n.confidence === 'low' ? '低置信' : '推断'}
                  </span>
                )}
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
                      <b>{n.confidence === 'high' ? '原文依据' : '引用上下文'}</b>
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
