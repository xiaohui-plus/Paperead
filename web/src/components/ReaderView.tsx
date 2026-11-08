import { useCallback, useEffect, useRef, useState } from 'react'
import type { Block, Paper, ViewMode } from '../types'
import { loadPaper } from '../lib/api'
import { outlineOf } from '../lib/blocks'
import { keyPoints } from '../lib/assistant'
import { translateStream } from '../lib/library'
import { isConfigured, loadSettings } from '../lib/settings'
import { TopBar } from './TopBar'
import { LeftPanel } from './LeftPanel'
import { Reader } from './Reader'
import { SettingsDialog } from './SettingsDialog'

interface Props {
  paperId: string
  onBack: () => void
}

export function ReaderView({ paperId, onBack }: Props) {
  const [paper, setPaper] = useState<Paper | null>(null)
  const [error, setError] = useState('')
  const [parsing, setParsing] = useState(false)
  const [mode, setMode] = useState<ViewMode>('bilingual')
  const [scale, setScale] = useState(1)
  const [activeId, setActiveId] = useState('')
  const [visiblePage, setVisiblePage] = useState(1)
  const [settledPage, setSettledPage] = useState(1)
  const [translating, setTranslating] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(
    () => new URLSearchParams(window.location.search).get('settings') === '1',
  )
  const [configured, setConfigured] = useState(() => isConfigured(loadSettings()))
  const scrollRef = useRef<HTMLDivElement>(null)
  const paperRef = useRef<Paper | null>(null)
  const pageById = useRef<Map<string, number>>(new Map())
  const requested = useRef<Set<number>>(new Set())
  const abortRef = useRef<AbortController | null>(null)
  const inFlight = useRef(false)
  const inFlightPages = useRef<number[]>([])
  const queued = useRef<number[] | null>(null)

  // ── 加载（解析没完成时自动重试） ───────────────────────
  useEffect(() => {
    let alive = true
    let timer: number | undefined
    setPaper(null)
    setError('')
    setVisiblePage(1)
    setSettledPage(1)
    requested.current.clear()

    const attempt = async (n: number) => {
      try {
        const p = await loadPaper(paperId)
        if (!alive) return
        setParsing(false)
        setPaper(p)
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        if (!alive) return
        // 409：还在解析中，等一会儿再试（最多约 90s）
        if (msg.includes('解析') && n < 60) {
          setParsing(true)
          timer = window.setTimeout(() => void attempt(n + 1), 1500)
          return
        }
        setParsing(false)
        setError(msg)
      }
    }
    void attempt(0)
    return () => {
      alive = false
      if (timer) window.clearTimeout(timer)
    }
  }, [paperId])

  useEffect(() => {
    paperRef.current = paper
    if (!paper) return
    const map = new Map<string, number>()
    paper.blocks.forEach((b) => map.set(b.id, b.page))
    pageById.current = map
  }, [paper])

  // ── 按页懒翻译：单飞 + 看门狗重试（流式，逐段出现） ──────
  const ensurePages = useCallback(
    async (pages: number[]) => {
      const current = paperRef.current
      if (!current || paperId === 'demo') return
      const cfg = loadSettings()
      if (!isConfigured(cfg)) return
      // 同一时刻只跑一条流。用户快速滑动时：若正在翻的页已经离开视野，
      // 直接中止它，把配额让给用户真正停下来的那一页；否则排到队尾。
      if (inFlight.current) {
        if (!inFlightPages.current.includes(pages[0])) {
          abortRef.current?.abort()
        }
        queued.current = pages
        return
      }

      const need = pages.filter((pg) => {
        if (requested.current.has(pg)) return false
        const blocks = current.blocks.filter(
          (b) => b.page === pg && b.translatable && b.text.trim(),
        )
        if (!blocks.length) return false
        return blocks.some((b) => !b.translated)
      })
      if (!need.length) return

      need.forEach((pg) => requested.current.add(pg))
      inFlight.current = true
      inFlightPages.current = need
      const ac = new AbortController()
      abortRef.current = ac
      setTranslating(true)
      try {
        await translateStream(
          paperId,
          { base_url: cfg.baseUrl, api_key: cfg.apiKey, model: cfg.model },
          need,
          (evt) => {
            if (evt.type === 'block' && evt.block) {
              const updated = evt.block as unknown as Block
              setPaper((prev) =>
                prev
                  ? {
                      ...prev,
                      blocks: prev.blocks.map((b) => (b.id === updated.id ? updated : b)),
                    }
                  : prev,
              )
            }
          },
          ac.signal,
        )
      } catch (e) {
        if (!(e instanceof DOMException && e.name === 'AbortError')) {
          console.warn('翻译流中断：', e)
        }
        // 失败/中断都撤销标记，交给看门狗重试
        need.forEach((pg) => requested.current.delete(pg))
      } finally {
        inFlight.current = false
        inFlightPages.current = []
        abortRef.current = null
        setTranslating(false)
        const next = queued.current
        queued.current = null
        if (next) window.setTimeout(() => void ensurePages(next), 0)
      }
    },
    [paperId],
  )

  // 进入阅读页先翻当前页 + 下一页；滚动停稳后换页继续增量翻
  useEffect(() => {
    if (!paper) return
    const maxPage = paper.page_count || Math.max(1, ...paper.blocks.map((b) => b.page))
    const pages = [settledPage, settledPage + 1].filter((p) => p >= 1 && p <= maxPage)
    void ensurePages(pages)
  }, [paper, settledPage, ensurePages])

  // 滚动停稳 700ms 才算「用户停留」，快速滑动过程中不触发翻译
  useEffect(() => {
    const timer = window.setTimeout(() => setSettledPage(visiblePage), 700)
    return () => window.clearTimeout(timer)
  }, [visiblePage])

  // 看门狗：每 3 秒检查当前页是否还有没翻的块。
  // 流被中断、429、网络抖动都能自愈，不会出现"一直转圈但不动"。
  useEffect(() => {
    if (paperId === 'demo') return
    const timer = window.setInterval(() => {
      if (inFlight.current) return
      const current = paperRef.current
      if (!current) return
      const cfg = loadSettings()
      if (!isConfigured(cfg)) return
      const maxPage = current.page_count || 1
      const pages = [settledPage, settledPage + 1].filter((p) => p >= 1 && p <= maxPage)
      const hasPending = pages.some((pg) =>
        current.blocks.some(
          (b) => b.page === pg && b.translatable && b.text.trim() && !b.translated,
        ),
      )
      if (hasPending) void ensurePages(pages)
    }, 3000)
    return () => window.clearInterval(timer)
  }, [settledPage, ensurePages, paperId])

  // ── 深链 / 目录高亮 / 当前页 ───────────────────────────
  useEffect(() => {
    if (!paper) return
    const hash = decodeURIComponent(window.location.hash.slice(1))
    if (!hash) return
    const timer = window.setTimeout(() => {
      const el = scrollRef.current
      const node = el?.querySelector<HTMLElement>(`#${CSS.escape(hash)}`)
      node?.scrollIntoView({ block: 'start', behavior: 'instant' as ScrollBehavior })
      // scrollIntoView 在部分浏览器不会触发 scroll 事件，手动派发一次让页码同步
      el?.dispatchEvent(new Event('scroll'))
    }, 120)
    return () => window.clearTimeout(timer)
  }, [paper])

  useEffect(() => {
    const el = scrollRef.current
    if (!el || !paper) return

    const onScroll = () => {
      const threshold = el.getBoundingClientRect().top + 140
      let heading = ''
      let page = 1
      el.querySelectorAll<HTMLElement>('[data-block-id], [data-heading-id]').forEach((node) => {
        if (node.getBoundingClientRect().top > threshold) return
        if (node.dataset.headingId) heading = node.dataset.headingId
        const p = pageById.current.get(node.id)
        if (p) page = p
      })
      setActiveId((prev) => (prev === heading ? prev : heading))
      setVisiblePage((prev) => (prev === page ? prev : page))
    }

    el.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => el.removeEventListener('scroll', onScroll)
  }, [paper])

  const jump = (id: string) => {
    const node = scrollRef.current?.querySelector<HTMLElement>(`#${CSS.escape(id)}`)
    node?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  if (error) {
    return (
      <div className="splash">
        <div>
          <p style={{ fontWeight: 600, color: 'var(--ink)' }}>加载失败</p>
          <p>{error}</p>
          <button className="btn" onClick={onBack} style={{ marginTop: 12 }}>
            返回文献库
          </button>
        </div>
      </div>
    )
  }

  if (!paper) {
    return (
      <div className="splash">
        <div>
          <div className="spinner" />
          {parsing ? '正在解析版面、图表和公式…' : '正在加载论文…'}
        </div>
      </div>
    )
  }

  const translated = paper.blocks.filter((b) => b.translated).length
  const total = paper.blocks.filter((b) => b.translatable && b.text.trim()).length
  const outline = outlineOf(paper.blocks)
  const points = keyPoints(paper.blocks)
  const sampleImage =
    paper.blocks.find((b) => b.kind === 'equation' && b.image_path)?.image_path ?? undefined

  return (
    <div className="app">
      <TopBar
        paper={paper}
        isDemo={paperId === 'demo'}
        mode={mode}
        onMode={setMode}
        scale={scale}
        onScale={(v) => setScale(Math.min(1.35, Math.max(0.85, v)))}
        translated={translated}
        total={total}
        onOpenSettings={() => {
          setSettingsOpen(true)
        }}
        configured={configured}
        onBack={onBack}
        hasTranslation={translated > 0}
        translating={translating}
        page={visiblePage}
      />

      <div className="workspace">
        <LeftPanel
          paperId={paperId}
          paper={paper}
          outline={outline}
          points={points}
          activeId={activeId}
          onJump={jump}
          onOpenSettings={() => setSettingsOpen(true)}
        />
        <Reader
          paper={paper}
          mode={mode}
          scale={scale}
          scrollRef={scrollRef}
          canTranslate={configured}
          onOpenSettings={() => setSettingsOpen(true)}
          hint={
            !configured
              ? '未配置模型，翻译未开启。填写模型与密钥后，打开论文会自动翻译当前页和下一页。'
              : translating
                ? '正在翻译当前页…'
                : '当前页还没有译文，停在这里几秒会自动开始翻译。'
          }
        />
      </div>

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        sampleImage={sampleImage}
        onSaved={() => {
          setConfigured(isConfigured(loadSettings()))
          requested.current.clear() // 换模型后允许重翻
        }}
      />
    </div>
  )
}
