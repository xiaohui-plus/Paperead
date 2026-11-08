import { useCallback, useEffect, useRef, useState } from 'react'
import { Sidebar } from './Sidebar'
import { SettingsDialog } from './SettingsDialog'
import { ScholarDaily } from './ScholarDaily'
import { ScholarDetail } from './ScholarDetail'
import { ScholarResult } from './ScholarResult'
import { ScholarSearchBar } from './ScholarSearchBar'
import {
  SORT_META,
  downloadText,
  exportScholarPapers,
  getDaily,
  getScholarPaper,
  importScholarPaper,
  searchPapers,
  type DailyResponse,
  type ScholarPaper,
  type SearchParams,
  type SearchResponse,
} from '../lib/scholar'
import { readParams, readPid, writeParams } from '../lib/scholarUrl'

interface Props {
  onOpenPaper: (id: string) => void
  onNav: (key: 'library' | 'search') => void
}

type Toast = { text: string; kind: 'ok' | 'err' } | null

function errorText(error: unknown): string {
  if (error instanceof TypeError) {
    return '连不上本地服务，请先运行 python -m server.main（或确认桌面版已启动）。'
  }
  return error instanceof Error ? error.message : '请求失败，请稍后重试。'
}

export function ScholarPage({ onOpenPaper, onNav }: Props) {
  const [initial] = useState(readParams)
  const [params, setParams] = useState<SearchParams>(initial)
  const [input, setInput] = useState(initial.q)
  const [pid, setPid] = useState<string | null>(() => readPid())
  const [advanced, setAdvanced] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [toast, setToast] = useState<Toast>(null)

  const [data, setData] = useState<SearchResponse | null>(null)
  const [searching, setSearching] = useState(false)

  const [daily, setDaily] = useState<DailyResponse | null>(null)
  const [dailyLoading, setDailyLoading] = useState(false)
  const [dailyRound, setDailyRound] = useState(0)

  const [detail, setDetail] = useState<ScholarPaper | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [importing, setImporting] = useState(false)

  const paramsRef = useRef(params)
  const pidRef = useRef(pid)
  const pushedDetail = useRef(false)
  const searchSeq = useRef(0)
  const mainRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    paramsRef.current = params
  }, [params])

  useEffect(() => {
    pidRef.current = pid
    if (pid) mainRef.current?.scrollTo({ top: 0 })
  }, [pid])

  const notify = (text: string, kind: 'ok' | 'err' = 'ok') => {
    setToast({ text, kind })
    window.setTimeout(() => setToast((current) => (current?.text === text ? null : current)), 4200)
  }

  // 地址栏变化（浏览器前进/后退）时同步状态
  useEffect(() => {
    const onPop = () => {
      const next = readParams()
      const nextPid = readPid()
      paramsRef.current = next
      pidRef.current = nextPid
      setParams(next)
      setInput(next.q)
      setPid(nextPid)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const update = useCallback((patch: Partial<SearchParams>) => {
    const next = { ...paramsRef.current, ...patch }
    if (!('page' in patch)) next.page = 0
    paramsRef.current = next
    setParams(next)
    writeParams(next, pidRef.current, 'replace')
    mainRef.current?.scrollTo({ top: 0 })
  }, [])

  // 输入停顿 450ms 自动检索；回车由表单立即提交
  useEffect(() => {
    const text = input.trim()
    if (text === params.q) return
    const timer = window.setTimeout(() => update({ q: text, page: 0 }), 450)
    return () => window.clearTimeout(timer)
  }, [input, params.q, update])

  const runSearch = useCallback((next: SearchParams) => {
    if (!next.q.trim()) {
      setData(null)
      setSearching(false)
      return
    }
    const seq = ++searchSeq.current
    setSearching(true)
    searchPapers(next)
      .then((result) => {
        if (seq === searchSeq.current) setData(result)
      })
      .catch((error) => {
        if (seq !== searchSeq.current) return
        setData({
          papers: [],
          total: 0,
          page_limit: null,
          used_query: next.q,
          translated: false,
          error: errorText(error),
        })
      })
      .finally(() => {
        if (seq === searchSeq.current) setSearching(false)
      })
  }, [])

  useEffect(() => {
    runSearch(params)
  }, [params, runSearch])

  useEffect(() => {
    if (params.q.trim()) return
    let alive = true
    setDailyLoading(true)
    getDaily(dailyRound)
      .then((result) => {
        if (alive) setDaily(result.error ? { ...result, papers: [] } : result)
      })
      .catch((error) => {
        if (alive) {
          setDaily({ papers: [], date: '', pool_size: 0, error: errorText(error) })
        }
      })
      .finally(() => {
        if (alive) setDailyLoading(false)
      })
    return () => {
      alive = false
    }
  }, [params.q, dailyRound])

  // 详情：pid 变了就取一次（列表里的数据先顶上，详情返回后替换）
  useEffect(() => {
    if (!pid) {
      setDetail(null)
      setDetailError('')
      return
    }
    let alive = true
    setDetailLoading(true)
    setDetailError('')
    getScholarPaper(pid)
      .then((paper) => {
        if (alive) setDetail(paper)
      })
      .catch((error) => {
        if (alive) {
          setDetail(null)
          setDetailError(errorText(error))
        }
      })
      .finally(() => {
        if (alive) setDetailLoading(false)
      })
    return () => {
      alive = false
    }
  }, [pid])

  const openDetail = (paper: ScholarPaper) => {
    setDetail(paper)
    setPid(paper.id)
    pidRef.current = paper.id
    pushedDetail.current = true
    writeParams(paramsRef.current, paper.id, 'push')
  }

  const closeDetail = () => {
    if (pushedDetail.current) {
      pushedDetail.current = false
      window.history.back()
      return
    }
    setPid(null)
    pidRef.current = null
    writeParams(paramsRef.current, null, 'replace')
  }

  const submit = () => {
    const text = input.trim()
    setInput(text)
    update({ q: text, page: 0 })
    if (mainRef.current) mainRef.current.scrollTop = 0
  }

  const clear = () => {
    setInput('')
    update({ q: '', page: 0 })
  }

  const onImport = async (paper: ScholarPaper) => {
    setImporting(true)
    try {
      const result = await importScholarPaper(paper.id)
      notify(result.already ? '这篇已在文献库，正在打开…' : '已导入文献库，正在解析版面…')
      onOpenPaper(result.paper.id)
    } catch (error) {
      notify(errorText(error), 'err')
    } finally {
      setImporting(false)
    }
  }

  const onExport = async (format: 'bibtex' | 'ris') => {
    if (!data?.papers.length) return
    try {
      const file = await exportScholarPapers(data.papers, format)
      downloadText(file.filename, file.text)
    } catch (error) {
      notify(errorText(error), 'err')
    }
  }

  const pages = data
    ? Math.min(
        Math.max(1, Math.ceil(data.total / params.perPage)),
        data.page_limit ?? 50,
      )
    : 0
  const highlightQuery = data?.translated ? data.used_query : params.q
  const hasQuery = params.q.trim().length > 0

  return (
    <div className="library">
      <Sidebar
        active="search"
        onNav={onNav}
        onSettings={() => setSettingsOpen(true)}
      />

      <main className="library-main scholar-main" ref={mainRef}>
        {pid ? (
          <ScholarDetail
            paper={detail}
            loading={detailLoading}
            error={detailError}
            onBack={closeDetail}
            onImport={onImport}
            importing={importing}
          />
        ) : (
          <>
            <ScholarSearchBar
              input={input}
              onInput={setInput}
              onSubmit={submit}
              onClear={clear}
              params={params}
              onChange={update}
              advanced={advanced}
              onToggleAdvanced={() => setAdvanced((value) => !value)}
              loading={searching}
              translatedQuery={data?.translated ? data.used_query : null}
            />

            {!hasQuery ? (
              <ScholarDaily
                data={daily}
                loading={dailyLoading}
                onRound={() => setDailyRound((round) => round + 1)}
                onOpen={openDetail}
              />
            ) : (
              <section className={`scholar-results ${searching && data ? 'is-updating' : ''}`}>
                <div className="scholar-results-head">
                  <span>
                    {data ? (
                      <>
                        共 <b>{data.total.toLocaleString()}</b> 条结果
                      </>
                    ) : (
                      '检索中…'
                    )}
                    {searching && data ? <em>更新中…</em> : null}
                  </span>
                  <div className="scholar-sorts">
                    {SORT_META.filter((s) => params.source === 'arxiv' || s.k !== 'updated').map(
                      (s) => (
                        <button
                          key={s.k}
                          className={params.sort === s.k ? 'is-on' : ''}
                          onClick={() => update({ sort: s.k })}
                        >
                          {s.l}
                        </button>
                      ),
                    )}
                  </div>
                </div>

                {data?.error && <p className="scholar-empty">{data.error}</p>}
                {!data && searching && <p className="scholar-empty">正在检索…</p>}
                {data && !data.error && data.papers.length === 0 && (
                  <p className="scholar-empty">没有找到相关文献，换个关键词试试。</p>
                )}

                {data?.papers.map((paper) => (
                  <ScholarResult
                    key={paper.id}
                    paper={paper}
                    query={highlightQuery}
                    onOpen={openDetail}
                  />
                ))}

                {data && data.papers.length > 0 && (
                  <footer className="scholar-foot">
                    <div className="scholar-export">
                      <button className="mini-btn" onClick={() => onExport('bibtex')}>
                        导出 BibTeX
                      </button>
                      <button className="mini-btn" onClick={() => onExport('ris')}>
                        导出 RIS
                      </button>
                    </div>

                    {pages > 1 && (
                      <div className="scholar-pager">
                        <button
                          className="mini-btn"
                          disabled={params.page <= 0}
                          onClick={() => update({ page: params.page - 1 })}
                        >
                          上一页
                        </button>
                        <span>
                          第 {params.page + 1} / {pages} 页
                        </span>
                        <button
                          className="mini-btn"
                          disabled={params.page + 1 >= pages}
                          onClick={() => update({ page: params.page + 1 })}
                        >
                          下一页
                        </button>
                      </div>
                    )}
                  </footer>
                )}
              </section>
            )}
          </>
        )}
      </main>

      {toast && (
        <div className={`toast ${toast.kind}`} onClick={() => setToast(null)}>
          {toast.text}
        </div>
      )}

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={() => notify('已保存模型配置')}
      />
    </div>
  )
}
