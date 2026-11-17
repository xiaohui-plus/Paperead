import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { browser } from 'wxt/browser'
import Markdown from './Markdown'
import {
  askSelection,
  askSelectionStream,
  AskStreamUnsupportedError,
  createReadingAnnotation,
  discoverBase,
  getPaper,
  getReadingAnnotations,
  getReadingGuide,
  getReadingHighlights,
  isPaired,
  makeReadingGuide,
  makeReadingHighlights,
  type ReadingAnnotation,
  type ReadingGuide,
  type ReadingHighlight,
  type ReadingHighlights,
} from '../../lib/api'
import type { PaperSummary } from '../../lib/types'

const ERROR_FALLBACK = '操作失败，请稍后重试'

function messageOf(error: unknown): string {
  return error instanceof Error && error.message ? error.message : ERROR_FALLBACK
}

function isPdfUrl(url: string): boolean {
  return /\.pdf($|[?#])/i.test(url)
}

function viewerFileOf(url: string): string | null {
  if (!url.includes('/viewer.html')) return null
  try {
    return new URL(url).searchParams.get('file') || null
  } catch {
    return null
  }
}

type Phase = 'pairing' | 'resolving' | 'ready' | 'empty' | 'error'

interface ContextState {
  phase: Phase
  message: string
  paper: PaperSummary | null
  tabId: number | null
}

function usePaperContext(): {
  context: ContextState
  tabUrl: string
  retry: () => void
} {
  const [context, setContext] = useState<ContextState>({
    phase: 'pairing',
    message: '正在连接 Paperead…',
    paper: null,
    tabId: null,
  })
  const [tabUrl, setTabUrl] = useState('')
  const [retryTick, setRetryTick] = useState(0)
  const generation = useRef(0)
  const requested = useRef(new Set<string>())
  const readyPapers = useRef(new Map<string, PaperSummary>())

  const retry = useCallback(() => {
    requested.current.clear()
    setRetryTick((value) => value + 1)
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const resolve = async () => {
      const current = ++generation.current
      const stale = () => cancelled || current !== generation.current

      const [tab] = await browser.tabs.query({ active: true, currentWindow: true })
      if (stale()) return
      const tabId = tab?.id ?? null
      const url = tab?.url || ''
      setTabUrl(url)

      const paired = await isPaired().catch(() => false)
      if (stale()) return
      if (!paired) {
        setContext({
          phase: 'pairing',
          message: '正在连接 Paperead…',
          paper: null,
          tabId,
        })
        return
      }

      if (!tabId || !url) {
        setContext({
          phase: 'empty',
          message: '请切换到论文网页或 PDF 页面',
          paper: null,
          tabId,
        })
        return
      }

      const file = viewerFileOf(url)
      if (file) {
        const key = `viewerPaper:${file}`
        const stored = await browser.storage.session.get(key)
        if (stale()) return
        const paperId = typeof stored[key] === 'string' ? (stored[key] as string) : ''
        if (!paperId) {
          setContext({
            phase: 'resolving',
            message: '正在准备论文…',
            paper: null,
            tabId,
          })
          return
        }
        try {
          const paper = await getPaper(paperId)
          if (stale()) return
          if (paper.status === 'ready') {
            setContext({ phase: 'ready', message: '', paper, tabId })
          } else if (paper.status === 'failed') {
            setContext({
              phase: 'error',
              message: 'PDF 解析失败，可稍后在 Paperead 文献库中重试',
              paper: null,
              tabId,
            })
          } else {
            setContext({
              phase: 'resolving',
              message: '正在解析论文…',
              paper: null,
              tabId,
            })
          }
        } catch (error) {
          if (stale()) return
          setContext({ phase: 'error', message: messageOf(error), paper: null, tabId })
        }
        return
      }

      if (!/^https?:\/\//i.test(url)) {
        setContext({
          phase: 'empty',
          message: '当前页面不是论文页面，请打开论文网页或 PDF',
          paper: null,
          tabId,
        })
        return
      }

      const key = `${tabId}|${url}`
      const cached = readyPapers.current.get(key)
      if (cached) {
        setContext({ phase: 'ready', message: '', paper: cached, tabId })
        return
      }
      if (requested.current.has(key)) {
        setContext((prev) =>
          prev.tabId === tabId && prev.paper
            ? prev
            : {
                phase: 'resolving',
                message: '正在识别并准备论文…',
                paper: null,
                tabId,
              },
        )
        return
      }
      requested.current.add(key)
      setContext({
        phase: 'resolving',
        message: '正在识别并准备论文…',
        paper: null,
        tabId,
      })

      try {
        const response = (await browser.runtime.sendMessage({
          type: 'ensure-paper',
          tabId,
        })) as { paper?: PaperSummary; error?: string }
        if (stale()) {
          requested.current.delete(key)
          return
        }
        if (response?.error) {
          requested.current.delete(key)
          setContext({ phase: 'error', message: response.error, paper: null, tabId })
          return
        }
        if (!response?.paper) {
          requested.current.delete(key)
          setContext({ phase: 'error', message: '没有识别到论文信息', paper: null, tabId })
          return
        }
        if (response.paper.status === 'ready') {
          readyPapers.current.set(key, response.paper)
          setContext({ phase: 'ready', message: '', paper: response.paper, tabId })
        } else if (response.paper.status === 'failed') {
          setContext({
            phase: 'error',
            message: '论文解析失败，可稍后在 Paperead 文献库中重试',
            paper: null,
            tabId,
          })
        } else {
          setContext({
            phase: 'resolving',
            message: '正在解析论文…',
            paper: null,
            tabId,
          })
        }
      } catch (error) {
        if (stale()) {
          requested.current.delete(key)
          return
        }
        requested.current.delete(key)
        setContext({ phase: 'error', message: messageOf(error), paper: null, tabId })
      }
    }

    const loop = async () => {
      await resolve()
      if (!cancelled) {
        timer = window.setTimeout(() => void loop(), 2000)
      }
    }
    void loop()

    const onTabChanged = () => {
      void resolve()
    }
    const onUpdated = (
      _tabId: number,
      change: { url?: string; status?: string },
    ) => {
      if (change.url || change.status === 'complete') void resolve()
    }
    const onStorageChanged = (
      changes: Record<string, unknown>,
      area: string,
    ) => {
      if (area !== 'session') return
      if (Object.keys(changes).some((key) => key.startsWith('viewerPaper:'))) {
        void resolve()
      }
    }

    browser.tabs.onActivated.addListener(onTabChanged)
    browser.tabs.onUpdated.addListener(onUpdated)
    browser.storage.onChanged.addListener(onStorageChanged)

    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
      browser.tabs.onActivated.removeListener(onTabChanged)
      browser.tabs.onUpdated.removeListener(onUpdated)
      browser.storage.onChanged.removeListener(onStorageChanged)
    }
  }, [retryTick])

  return { context, tabUrl, retry }
}

const TABS = [
  { key: 'guide', label: '导读' },
  { key: 'ask', label: '问答' },
  { key: 'highlights', label: '自动阅读' },
  { key: 'annotations', label: '批注' },
] as const

type TabKey = (typeof TABS)[number]['key']

function StateBlock({
  title,
  detail,
  action,
}: {
  title: string
  detail?: string
  action?: ReactNode
}) {
  return (
    <div className="state">
      <p className="state-title">{title}</p>
      {detail && <p className="hint">{detail}</p>}
      {action}
    </div>
  )
}

function ContextStatus({
  context,
  onRetry,
}: {
  context: ContextState
  onRetry: () => void
}) {
  const busy = context.phase === 'pairing' || context.phase === 'resolving'
  if (busy) {
    return (
      <div className="state">
        <span className="spinner" aria-hidden="true" />
        <p className="state-title">{context.message}</p>
        <p className="hint">
          {context.phase === 'pairing'
            ? '请打开 Paperead 桌面端，连接会自动完成，无需手动配对。'
            : '正在提取元数据并送入本地文献库，首次解析可能需要一会儿。'}
        </p>
      </div>
    )
  }
  return (
    <StateBlock
      title={context.phase === 'empty' ? '没有可阅读的论文' : '暂时无法准备这篇论文'}
      detail={context.message}
      action={
        <button type="button" className="btn" onClick={onRetry}>
          重试
        </button>
      }
    />
  )
}

const GLOSSARY_KIND: Record<string, string> = {
  term: '术语',
  method: '方法',
  dataset: '数据集',
  metric: '指标',
  abbr: '缩写',
}

function GuideTab({ paperId }: { paperId: string }) {
  const [guide, setGuide] = useState<ReadingGuide | null>(null)
  const [status, setStatus] = useState<'loading' | 'empty' | 'ready' | 'error'>(
    'loading',
  )
  const [error, setError] = useState('')
  const [generating, setGenerating] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    setError('')
    getReadingGuide(paperId)
      .then((result) => {
        if (cancelled) return
        if (result.guide) {
          setGuide(result.guide)
          setStatus('ready')
        } else {
          setGuide(null)
          setStatus('empty')
        }
      })
      .catch((cause) => {
        if (cancelled) return
        setError(messageOf(cause))
        setStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [paperId, reload])

  const generate = useCallback(async () => {
    setGenerating(true)
    setError('')
    try {
      const result = await makeReadingGuide(paperId)
      if (result) {
        setGuide(result)
        setStatus('ready')
      } else {
        setError('导读生成失败，请稍后重试')
        setStatus('error')
      }
    } catch (cause) {
      const message = messageOf(cause)
      setError(
        message.includes('正在生成中')
          ? '导读正在生成中，稍后再打开这个页签即可'
          : message,
      )
      setStatus('error')
    } finally {
      setGenerating(false)
    }
  }, [paperId])

  if (status === 'loading' || generating) {
    return (
      <div className="state">
        <span className="spinner" aria-hidden="true" />
        <p className="state-title">
          {generating ? '正在生成导读…' : '正在读取导读…'}
        </p>
        {generating && <p className="hint">概括内容 → 梳理结构 → 提取术语</p>}
      </div>
    )
  }

  if (status === 'error') {
    return (
      <StateBlock
        title="导读加载失败"
        detail={error}
        action={
          <button type="button" className="btn" onClick={() => void generate()}>
            重试
          </button>
        }
      />
    )
  }

  if (status === 'empty' || !guide) {
    return (
      <StateBlock
        title="还没有导读"
        detail="生成后会有一句话概括、阅读顺序和术语表，帮你快速抓住这篇论文。"
        action={
          <button type="button" className="btn primary" onClick={() => void generate()}>
            生成导读
          </button>
        }
      />
    )
  }

  const oneliner =
    typeof guide.oneliner === 'string' ? guide.oneliner.trim() : ''
  const summary = typeof guide.summary === 'string' ? guide.summary.trim() : ''
  const path = Array.isArray(guide.path) ? guide.path.filter(Boolean) : []
  const glossary = Array.isArray(guide.glossary)
    ? guide.glossary.filter(Boolean)
    : []
  const outline = Array.isArray(guide.outline)
    ? guide.outline.filter(Boolean)
    : []
  const questions = Array.isArray(guide.questions)
    ? guide.questions.filter(
        (item): item is string => typeof item === 'string' && Boolean(item.trim()),
      )
    : []
  const prereqs = Array.isArray(guide.prereqs)
    ? guide.prereqs.filter(Boolean)
    : []
  const domains = Array.isArray(guide.domains)
    ? guide.domains.filter(
        (item): item is string => typeof item === 'string' && Boolean(item.trim()),
      )
    : []
  const overview = guide.overview || {}
  const difficulty = guide.difficulty || {}
  const contributions = Array.isArray(overview.contributions)
    ? overview.contributions.filter(Boolean)
    : []
  const findings = Array.isArray(overview.findings)
    ? overview.findings.filter(Boolean)
    : []
  const limitations = Array.isArray(overview.limitations)
    ? overview.limitations.filter(Boolean)
    : []
  const whyItems = path.filter((step) => Boolean(step?.why))

  return (
    <div className="guide">
      {(oneliner || summary) && (
        <section className="block">
          {oneliner && <p className="guide-oneliner">{oneliner}</p>}
          {summary && summary !== oneliner && (
            <p className="guide-summary">{summary}</p>
          )}
          {domains.length > 0 && (
            <div className="chip-row">
              {domains.map((domain) => (
                <span className="chip" key={domain}>
                  {domain}
                </span>
              ))}
            </div>
          )}
          <p className="stat-row">
            {typeof difficulty.stars === 'number' && (
              <span>难度 {difficulty.stars} 星</span>
            )}
            {typeof difficulty.minutes === 'number' && (
              <span>预计 {difficulty.minutes} 分钟</span>
            )}
            {typeof difficulty.math === 'string' && difficulty.math && (
              <span>数学 · {difficulty.math}</span>
            )}
          </p>
        </section>
      )}

      <section className="block">
        <h3>为什么读 · 背景</h3>
        {overview.problem && <p>{overview.problem}</p>}
        {overview.approach && (
          <p>
            <b>做法：</b>
            {overview.approach}
          </p>
        )}
        {contributions.length > 0 && (
          <div>
            <p className="muted">主要贡献</p>
            <ul className="plain-list">
              {contributions.map((item, index) => (
                <li key={`contribution-${index}`}>
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {findings.length > 0 && (
          <div>
            <p className="muted">主要发现</p>
            <ul className="plain-list">
              {findings.map((item, index) => (
                <li key={`finding-${index}`}>
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {limitations.length > 0 && (
          <div>
            <p className="muted">局限</p>
            <ul className="plain-list">
              {limitations.map((item, index) => (
                <li key={`limitation-${index}`}>
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {whyItems.length > 0 && (
          <ul className="plain-list">
            {whyItems.map((step, index) => (
              <li key={`${step?.title || 'step'}-${index}`}>
                {step?.title && <b>{step.title}</b>}
                <span>{step?.why}</span>
              </li>
            ))}
          </ul>
        )}
        {!overview.problem &&
          !overview.approach &&
          contributions.length === 0 &&
          findings.length === 0 &&
          limitations.length === 0 &&
          whyItems.length === 0 && <p className="hint">导读没有提供背景信息。</p>}
      </section>

      {path.length > 0 ? (
        <section className="block">
          <h3>阅读顺序</h3>
          <ol className="path-list">
            {path.map((step, index) => (
              <li key={`${step?.title || 'path'}-${index}`}>
                <div className="path-head">
                  <b>{step?.title || `第 ${index + 1} 步`}</b>
                  <span className="muted">
                    {typeof step?.page === 'number' && step.page > 0
                      ? `第 ${step.page} 页`
                      : step?.section || ''}
                    {typeof step?.minutes === 'number' && (
                      <>
                        {(typeof step?.page === 'number' || step?.section) && ' · '}
                        {step.minutes} 分钟
                      </>
                    )}
                  </span>
                </div>
                {step?.concept && <p className="muted">为「{step.concept}」打底</p>}
                {step?.why && <p className="muted">为什么读：{step.why}</p>}
                {step?.order && <p className="muted">为什么排这里：{step.order}</p>}
                {step?.gain && <p className="muted">读完收获：{step.gain}</p>}
              </li>
            ))}
          </ol>
        </section>
      ) : (
        outline.length > 0 && (
          <section className="block">
            <h3>论文结构</h3>
            <ol className="path-list">
              {outline.map((item, index) => (
                <li key={`${item?.section || 'section'}-${index}`}>
                  <b>{item?.section || `第 ${index + 1} 节`}</b>
                  {item?.what && <p className="muted">{item.what}</p>}
                  {item?.focus && <p className="muted">关注：{item.focus}</p>}
                </li>
              ))}
            </ol>
          </section>
        )
      )}

      {prereqs.length > 0 && (
        <section className="block">
          <h3>看不懂时先补这些</h3>
          <ul className="plain-list">
            {prereqs.map((item, index) => (
              <li key={`${item?.concept || 'prereq'}-${index}`}>
                <b>{item?.concept || item?.en || '前置概念'}</b>
                {item?.why && <span>{item.why}</span>}
                {item?.paper?.title && (
                  <span className="muted">
                    {item.paper.year ? `${item.paper.year} · ` : ''}
                    {item.paper.title}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {glossary.length > 0 && (
        <details className="block term-box" open>
          <summary>
            术语表 <span className="muted">{glossary.length} 个</span>
          </summary>
          <dl className="glossary">
            {glossary.map((term, index) => (
              <div key={`${term?.en || 'term'}-${index}`}>
                <dt>
                  {term?.en || term?.zh || '术语'}
                  {term?.zh && term?.en && (
                    <span className="muted"> · {term.zh}</span>
                  )}
                  {term?.kind && (
                    <span className="term-kind">
                      {GLOSSARY_KIND[term.kind] || term.kind}
                    </span>
                  )}
                </dt>
                {term?.explain && <dd>{term.explain}</dd>}
                {term?.where && (
                  <dd className="muted">出现于 {term.where}</dd>
                )}
              </div>
            ))}
          </dl>
        </details>
      )}

      {questions.length > 0 && (
        <section className="block">
          <h3>建议问题</h3>
          <ul className="question-list">
            {questions.map((question, index) => (
              <li key={`question-${index}`}>{question}</li>
            ))}
          </ul>
        </section>
      )}

      {guide.degraded && (
        <p className="hint">模型未返回结构化结果，部分内容已降级展示。</p>
      )}

      <button type="button" className="btn subtle" onClick={() => setReload((v) => v + 1)}>
        重新读取
      </button>
    </div>
  )
}

interface HighlightGroup {
  key: string
  label: string
  items: Array<{ text: string; page?: number }>
}

const CATEGORY_LABELS: Record<string, string> = {
  objective: '研究目标',
  method: '方法',
  conclusion: '结论',
}
const CATEGORY_ORDER = ['objective', 'method', 'conclusion']

function normalizeHighlights(data: ReadingHighlights | null): HighlightGroup[] {
  const raw = data?.highlights
  if (!raw) return []

  const buckets = new Map<string, Array<{ text: string; page?: number }>>()
  const push = (category: string, item: ReadingHighlight | string) => {
    const text =
      typeof item === 'string'
        ? item.trim()
        : String(
            item.text ??
              item.quote_zh ??
              item.quote_en ??
              item.quote ??
              item.content ??
              '',
          ).trim()
    if (!text) return
    const key = category || 'other'
    const list = buckets.get(key) || []
    list.push({
      text,
      page:
        typeof item === 'object' && typeof item.page === 'number'
          ? item.page
          : undefined,
    })
    buckets.set(key, list)
  }

  if (Array.isArray(raw)) {
    raw.forEach((item) => {
      if (typeof item === 'string') {
        push('other', item)
        return
      }
      push(String(item.category ?? item.kind ?? 'other'), item)
    })
  } else {
    Object.entries(raw).forEach(([key, value]) => {
      if (!Array.isArray(value)) return
      value.forEach((item) => push(key, item))
    })
  }

  return [...buckets.entries()]
    .map(([key, items]) => ({
      key,
      label: CATEGORY_LABELS[key] || key,
      items,
    }))
    .sort((a, b) => {
      const ai = CATEGORY_ORDER.indexOf(a.key)
      const bi = CATEGORY_ORDER.indexOf(b.key)
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi)
    })
}

function needsTranslationOf(data: ReadingHighlights | null): boolean {
  if (!data) return false
  if (data.needs_translation === true) return true
  const pages = data.translated_pages
  if (typeof pages === 'number') return pages === 0
  if (Array.isArray(pages)) return pages.length === 0
  return false
}

function HighlightsTab({ paperId }: { paperId: string }) {
  const [data, setData] = useState<ReadingHighlights | null>(null)
  const [status, setStatus] = useState<'loading' | 'empty' | 'ready' | 'error'>(
    'loading',
  )
  const [error, setError] = useState('')
  const [generating, setGenerating] = useState(false)
  const [needsTranslation, setNeedsTranslation] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    setError('')
    getReadingHighlights(paperId)
      .then((result) => {
        if (cancelled) return
        setData(result)
        setNeedsTranslation(needsTranslationOf(result))
        setStatus(normalizeHighlights(result).length > 0 ? 'ready' : 'empty')
      })
      .catch((cause) => {
        if (cancelled) return
        setError(messageOf(cause))
        setStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [paperId, reload])

  const generate = useCallback(async () => {
    setGenerating(true)
    setError('')
    try {
      const result = await makeReadingHighlights(paperId)
      setData(result)
      setNeedsTranslation(needsTranslationOf(result))
      setStatus(normalizeHighlights(result).length > 0 ? 'ready' : 'empty')
    } catch (cause) {
      setError(messageOf(cause))
      setStatus('error')
    } finally {
      setGenerating(false)
    }
  }, [paperId])

  const openReader = useCallback(async () => {
    try {
      const base = await discoverBase()
      await browser.tabs.create({
        url: `${base}/?p=${encodeURIComponent(paperId)}`,
      })
    } catch (cause) {
      setError(messageOf(cause))
    }
  }, [paperId])

  const groups = useMemo(() => normalizeHighlights(data), [data])

  if (status === 'loading' || generating) {
    return (
      <div className="state">
        <span className="spinner" aria-hidden="true" />
        <p className="state-title">
          {generating ? '正在生成阅读重点…' : '正在读取阅读重点…'}
        </p>
        {generating && <p className="hint">从译文中提取研究目标、方法与结论</p>}
      </div>
    )
  }

  if (status === 'error') {
    return (
      <StateBlock
        title="阅读重点加载失败"
        detail={error}
        action={
          <button type="button" className="btn" onClick={() => setReload((v) => v + 1)}>
            重试
          </button>
        }
      />
    )
  }

  return (
    <div className="highlights">
      {needsTranslation && (
        <div className="notice">
          <b>需要先在 Paperead 阅读页生成翻译</b>
          <p>阅读重点依赖译文，生成后回到这里重新生成即可。</p>
          <button type="button" className="btn primary" onClick={() => void openReader()}>
            打开阅读页
          </button>
        </div>
      )}

      {groups.length === 0 ? (
        needsTranslation ? null : (
          <StateBlock
            title="还没有阅读重点"
            detail="自动阅读会列出研究目标、方法和结论三类重点。"
            action={
              <button type="button" className="btn primary" onClick={() => void generate()}>
                生成阅读重点
              </button>
            }
          />
        )
      ) : (
        groups.map((group) => (
          <section className="block" key={group.key}>
            <h3>{group.label}</h3>
            <ul className="highlight-list">
              {group.items.map((item, index) => (
                <li key={`${group.key}-${index}`}>
                  <p>{item.text}</p>
                  {typeof item.page === 'number' && item.page > 0 && (
                    <span className="muted">第 {item.page} 页</span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        ))
      )}

      {groups.length > 0 && (
        <button type="button" className="btn subtle" onClick={() => void generate()}>
          重新生成
        </button>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  )
}

function AnnotationsTab({ paperId, tabId }: { paperId: string; tabId: number | null }) {
  const [items, setItems] = useState<ReadingAnnotation[]>([])
  const [status, setStatus] = useState<'loading' | 'empty' | 'ready' | 'error'>(
    'loading',
  )
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [quote, setQuote] = useState('')
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    setError('')
    getReadingAnnotations(paperId)
      .then((annotations) => {
        if (cancelled) return
        setItems(annotations)
        setStatus(annotations.length > 0 ? 'ready' : 'empty')
      })
      .catch((cause) => {
        if (cancelled) return
        setError(messageOf(cause))
        setStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [paperId, reload])

  const captureSelection = useCallback(async () => {
    if (!tabId) {
      setError('当前没有可读取选区的标签页')
      return
    }
    try {
      const [result] = await browser.scripting.executeScript({
        target: { tabId },
        func: () => window.getSelection()?.toString() || '',
      })
      const text = String(result?.result || '').trim()
      if (!text) {
        setError('页面上没有选中文字，请先在论文中选中一段再引用')
        return
      }
      setQuote(text.slice(0, 4000))
      setError('')
      setNotice('已引用当前选中的文字')
    } catch {
      setError('无法读取页面选区，请先在论文页面中选中文字再试')
    }
  }, [tabId])

  const save = useCallback(async () => {
    const trimmedQuote = quote.trim()
    if (!trimmedQuote) {
      setError('请先填写或引用要批注的原文')
      return
    }
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const created = await createReadingAnnotation(
        paperId,
        trimmedQuote,
        note.trim(),
      )
      if (created) {
        setItems((prev) => [created, ...prev])
        setStatus('ready')
      } else {
        setReload((value) => value + 1)
      }
      setQuote('')
      setNote('')
      setNotice('批注已保存')
    } catch (cause) {
      setError(messageOf(cause))
    } finally {
      setSaving(false)
    }
  }, [paperId, quote, note])

  return (
    <div className="annotations">
      <section className="composer">
        <div className="composer-head">
          <h3>新建批注</h3>
          <button type="button" className="btn subtle" onClick={() => void captureSelection()}>
            引用当前选中文字
          </button>
        </div>
        <label className="field">
          <span>原文</span>
          <textarea
            value={quote}
            rows={3}
            maxLength={4000}
            placeholder="选中论文里的文字，或直接粘贴要批注的内容"
            onChange={(event) => setQuote(event.target.value)}
          />
        </label>
        <label className="field">
          <span>批注</span>
          <textarea
            value={note}
            rows={2}
            maxLength={1000}
            placeholder="写下你的理解或疑问（可选）"
            onChange={(event) => setNote(event.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn primary"
          disabled={saving}
          onClick={() => void save()}
        >
          {saving ? '保存中…' : '保存批注'}
        </button>
        {notice && <p className="notice-text">{notice}</p>}
        {error && <p className="error">{error}</p>}
      </section>

      <section className="block">
        <h3>全部批注</h3>
        {status === 'loading' && (
          <div className="state compact">
            <span className="spinner" aria-hidden="true" />
            <p className="state-title">正在读取批注…</p>
          </div>
        )}
        {status === 'error' && (
          <StateBlock
            title="批注加载失败"
            detail={error}
            action={
              <button type="button" className="btn" onClick={() => setReload((v) => v + 1)}>
                重试
              </button>
            }
          />
        )}
        {status === 'empty' && (
          <p className="hint">还没有批注。引用一段原文，写下你的想法吧。</p>
        )}
        {items.length > 0 && (
          <ul className="annotation-list">
            {items.map((item, index) => (
              <li key={item.id || `${index}-${item.created_at || ''}`}>
                <blockquote>{item.quote}</blockquote>
                {item.note && <p className="annotation-note">{item.note}</p>}
                {typeof item.page === 'number' && item.page > 0 && (
                  <span className="muted">第 {item.page} 页</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

interface ChatSource {
  label: string
  url?: string
  page?: number
}

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  sources?: ChatSource[]
  failed?: boolean
  stopped?: boolean
}

function nextId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function describeSource(item: unknown): ChatSource {
  if (typeof item === 'string') return { label: item.trim() || '出处' }
  if (item && typeof item === 'object') {
    const data = item as Record<string, unknown>
    const label = String(
      data.title ??
        data.label ??
        data.section ??
        data.quote ??
        data.text ??
        data.source ??
        '',
    ).trim()
    const page = typeof data.page === 'number' ? data.page : undefined
    return {
      label: label || (page ? `第 ${page} 页` : '出处'),
      url: typeof data.url === 'string' ? data.url : undefined,
      page,
    }
  }
  return { label: '出处' }
}

function ChatTab({ paperId }: { paperId: string }) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streamingId, setStreamingId] = useState<string | null>(null)
  const [statusText, setStatusText] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const streaming = streamingId !== null

  useEffect(() => {
    const element = scrollRef.current
    if (element) element.scrollTop = element.scrollHeight
  }, [messages, streamingId, statusText])

  const patchMessage = useCallback(
    (id: string, patch: Partial<ChatMessage>) => {
      setMessages((prev) =>
        prev.map((item) => (item.id === id ? { ...item, ...patch } : item)),
      )
    },
    [],
  )

  const appendDelta = useCallback((id: string, chunk: string) => {
    if (!chunk) return
    setMessages((prev) =>
      prev.map((item) =>
        item.id === id ? { ...item, text: item.text + chunk } : item,
      ),
    )
  }, [])

  const send = useCallback(async () => {
    const question = input.trim()
    if (!question || streaming) return
    setInput('')
    setStatusText('')
    const assistantId = nextId()
    const userMessage: ChatMessage = {
      id: nextId(),
      role: 'user',
      text: question,
    }
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: 'assistant',
      text: '',
    }
    setMessages((prev) => [...prev, userMessage, assistantMessage].slice(-20))
    setStreamingId(assistantId)
    const controller = new AbortController()
    abortRef.current = controller
    let received = false
    let failed = false

    try {
      await askSelectionStream(
        paperId,
        question,
        {
          onStatus: (status) => {
            if (!received && status) setStatusText(status)
          },
          onSources: (sources) => {
            patchMessage(assistantId, { sources: sources.map(describeSource) })
          },
          onDelta: (text) => {
            received = true
            if (text) appendDelta(assistantId, text)
          },
          onError: (detail) => {
            failed = true
            patchMessage(assistantId, { text: detail, failed: true })
          },
          onDone: () => {},
        },
        controller.signal,
      )
      if (!received && !failed) {
        patchMessage(assistantId, {
          text: controller.signal.aborted ? '已停止生成' : '没有返回答案，请换个问法试试',
          stopped: controller.signal.aborted,
        })
      }
    } catch (cause) {
      if (cause instanceof AskStreamUnsupportedError) {
        try {
          const result = await askSelection(paperId, question)
          patchMessage(assistantId, {
            text: result.answer || '没有返回答案，请换个问法试试',
            sources: Array.isArray(result.sources)
              ? result.sources.map(describeSource)
              : undefined,
          })
        } catch (error) {
          patchMessage(assistantId, {
            text: messageOf(error),
            failed: true,
          })
        }
      } else if (controller.signal.aborted) {
        setMessages((prev) =>
          prev.map((item) =>
            item.id === assistantId
              ? {
                  ...item,
                  text: item.text || '已停止生成',
                  stopped: true,
                }
              : item,
          ),
        )
      } else {
        setMessages((prev) =>
          prev.map((item) =>
            item.id === assistantId
              ? {
                  ...item,
                  text: item.text
                    ? `${item.text}\n\n（中断：${messageOf(cause)}）`
                    : messageOf(cause),
                  failed: true,
                }
              : item,
          ),
        )
      }
    } finally {
      abortRef.current = null
      setStreamingId(null)
      setStatusText('')
    }
  }, [paperId, input, streaming, patchMessage, appendDelta])

  const stop = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return (
    <div className="chat">
      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 && !streaming && (
          <div className="state compact">
            <p className="state-title">基于这篇论文提问</p>
            <p className="hint">
              可以问方法细节、实验结果，或让它解释某一段论证。
            </p>
          </div>
        )}
        {messages.map((item) => (
          <div
            key={item.id}
            className={`bubble ${item.role}${item.failed ? ' failed' : ''}${
              streamingId === item.id ? ' streaming' : ''
            }`}
          >
            {item.role === 'assistant' && !item.failed ? (
              <Markdown text={item.text} />
            ) : (
              item.text
            )}
            {streamingId === item.id && !item.text && (
              <span className="pending-line">
                <span className="spinner small" aria-hidden="true" />
                {statusText || '正在阅读论文并回答…'}
              </span>
            )}
            {item.sources && item.sources.length > 0 && (
              <div className="sources">
                {item.sources.map((source, index) => (
                  <span
                    className="source-chip"
                    key={`${item.id}-source-${index}`}
                    title={source.url || source.label}
                  >
                    {source.page ? `第 ${source.page} 页 · ` : ''}
                    {source.label}
                  </span>
                ))}
              </div>
            )}
            {item.stopped && !item.failed && (
              <span className="muted">（已停止）</span>
            )}
          </div>
        ))}
      </div>
      <div className="composer chat-composer">
        <textarea
          value={input}
          rows={2}
          aria-label="向论文提问"
          placeholder="问点什么…（Enter 发送，Shift+Enter 换行）"
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              void send()
            }
          }}
        />
        {streaming ? (
          <button type="button" className="btn" onClick={stop}>
            停止
          </button>
        ) : (
          <button
            type="button"
            className="btn primary"
            disabled={!input.trim()}
            onClick={() => void send()}
          >
            发送
          </button>
        )}
      </div>
    </div>
  )
}

export default function App() {
  const { context, tabUrl, retry } = usePaperContext()
  const [tab, setTab] = useState<TabKey>('guide')
  const [openingViewer, setOpeningViewer] = useState(false)
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])

  const showViewerButton =
    context.tabId !== null && isPdfUrl(tabUrl) && !viewerFileOf(tabUrl)

  const openInViewer = useCallback(async () => {
    if (context.tabId === null || !isPdfUrl(tabUrl)) return
    setOpeningViewer(true)
    try {
      await browser.tabs.update(context.tabId, {
        url: browser.runtime.getURL(
          `/viewer.html?file=${encodeURIComponent(tabUrl)}`,
        ),
      })
    } catch {
      // 标签页已关闭或不允许导航时保持原样
    } finally {
      setOpeningViewer(false)
    }
  }, [context.tabId, tabUrl])

  const onTabKeyDown = (event: ReactKeyboardEvent, index: number) => {
    let next = -1
    if (event.key === 'ArrowRight') next = (index + 1) % TABS.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + TABS.length) % TABS.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = TABS.length - 1
    if (next < 0) return
    const nextTab = TABS[next]
    if (!nextTab) return
    event.preventDefault()
    setTab(nextTab.key)
    tabRefs.current[next]?.focus()
  }

  const dotClass =
    context.phase === 'ready'
      ? 'ready'
      : context.phase === 'error' || context.phase === 'empty'
        ? 'off'
        : 'wait'

  const statusLabel =
    context.phase === 'ready'
      ? '已连接并识别到论文'
      : context.phase === 'pairing'
        ? '正在连接 Paperead'
        : context.phase === 'resolving'
          ? '正在准备论文'
          : '未识别到论文'

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-row">
          <h1>阅读助手</h1>
          <span
            className={`status-dot ${dotClass}`}
            role="img"
            aria-label={statusLabel}
            title={statusLabel}
          />
        </div>
        <p className="paper-line" title={context.paper?.title || context.message}>
          {context.paper?.title || context.message}
        </p>
      </header>

      {showViewerButton && (
        <div className="pdf-banner">
          <p>当前是 PDF，可在 Paperead 阅读器里划词翻译与提问。</p>
          <button
            type="button"
            className="btn primary"
            disabled={openingViewer}
            onClick={() => void openInViewer()}
          >
            {openingViewer ? '正在打开…' : '用 Paperead 阅读器打开'}
          </button>
        </div>
      )}

      <div className="tabs" role="tablist" aria-label="阅读助手功能">
        {TABS.map((item, index) => (
          <button
            key={item.key}
            ref={(element) => {
              tabRefs.current[index] = element
            }}
            id={`tab-${item.key}`}
            type="button"
            role="tab"
            aria-selected={tab === item.key}
            aria-controls={`panel-${item.key}`}
            tabIndex={tab === item.key ? 0 : -1}
            className={`tab${tab === item.key ? ' active' : ''}`}
            onClick={() => setTab(item.key)}
            onKeyDown={(event) => onTabKeyDown(event, index)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {context.paper ? (
        <section
          id={`panel-${tab}`}
          role="tabpanel"
          aria-labelledby={`tab-${tab}`}
          tabIndex={0}
          className={`panel${tab === 'ask' ? ' chat-panel' : ''}`}
        >
          {tab === 'guide' && <GuideTab paperId={context.paper.id} />}
          {tab === 'ask' && <ChatTab paperId={context.paper.id} />}
          {tab === 'highlights' && <HighlightsTab paperId={context.paper.id} />}
          {tab === 'annotations' && (
            <AnnotationsTab paperId={context.paper.id} tabId={context.tabId} />
          )}
        </section>
      ) : (
        <section
          id={`panel-${tab}`}
          role="tabpanel"
          aria-labelledby={`tab-${tab}`}
          tabIndex={0}
          className="panel"
        >
          <ContextStatus context={context} onRetry={retry} />
        </section>
      )}
    </div>
  )
}
