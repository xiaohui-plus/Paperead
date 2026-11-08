import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import type { Block, Paper, ViewMode } from '../types'
import { loadPaper } from '../lib/api'
import { outlineOf } from '../lib/blocks'
import { keyPoints } from '../lib/assistant'
import { translateStream } from '../lib/library'
import {
  loadModelSettings,
  readyModel,
  toPayload,
  type ModelKind,
} from '../lib/settings'
import { TopBar } from './TopBar'
import { LeftPanel } from './LeftPanel'
import { PdfPages } from './PdfPages'
import { Reader } from './Reader'
import { SettingsDialog } from './SettingsDialog'

interface Props {
  paperId: string
  onBack: () => void
}

const PANEL_WIDTH_KEY = 'paperead.panelWidth'
const MIN_PANEL_WIDTH = 260
const MAX_PANEL_WIDTH = 640
const DEFAULT_PANEL_WIDTH = 344

function readPanelWidth(): number {
  try {
    const width = Number(localStorage.getItem(PANEL_WIDTH_KEY))
    if (
      Number.isFinite(width) &&
      width >= MIN_PANEL_WIDTH &&
      width <= MAX_PANEL_WIDTH
    ) {
      return width
    }
  } catch {
    // 隐私模式读不到 localStorage：用默认宽度
  }
  return DEFAULT_PANEL_WIDTH
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
  // 示例数据只有 paper.json，没有 PDF 可看
  const pdfHref = `/api/papers/${encodeURIComponent(paperId)}/file`
  const canShowPdf = paperId !== 'demo'
  const [pdfMode, setPdfMode] = useState(
    () =>
      paperId !== 'demo' &&
      new URLSearchParams(window.location.search).get('view') === 'pdf',
  )
  // 原版 PDF 的显示宽度与当前页码；页码跟着 PDF 滚动更新，顶栏显示 P{n}
  const initialExpanded =
    new URLSearchParams(window.location.search).get('full') === '1'
  const [pdfWidth, setPdfWidth] = useState(initialExpanded ? 1200 : 1000)
  const [pdfPage, setPdfPage] = useState(1)
  // 默认只占右侧阅读区、保留左侧功能面板；展开后才占满整行
  const [pdfExpanded, setPdfExpanded] = useState(initialExpanded)
  // 面板里点目录/重点/图表时，PDF 视图要滚到对应页
  const [pdfJump, setPdfJump] = useState<{ page: number; token: number } | null>(null)
  const jumpSeq = useRef(0)
  // 设置只由用户点齿轮/侧栏打开；刷新页面不自动弹
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [configured, setConfigured] = useState(() =>
    Boolean(readyModel(loadModelSettings(), 'answer')),
  )
  const [settingsKind, setSettingsKind] = useState<ModelKind>('answer')
  const [settingsVersion, setSettingsVersion] = useState(0)
  const [panelWidth, setPanelWidth] = useState(readPanelWidth)
  const [resizingPanel, setResizingPanel] = useState(false)
  const panelWidthRef = useRef(panelWidth)
  const dragRef = useRef<{
    startX: number
    startWidth: number
    zoom: number
  } | null>(null)
  const workspaceRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const savedScroll = useRef(0)
  const paperRef = useRef<Paper | null>(null)
  const pageById = useRef<Map<string, number>>(new Map())
  const requested = useRef<Set<number>>(new Set())
  const abortRef = useRef<AbortController | null>(null)
  const inFlight = useRef(false)
  const inFlightPages = useRef<number[]>([])
  const queued = useRef<number[] | null>(null)

  // ── 阅读助手宽度：拖动中间分隔线调整 ───────────────────
  const maxPanelWidth = () => {
    const total = workspaceRef.current?.clientWidth || window.innerWidth
    return Math.max(
      MIN_PANEL_WIDTH,
      Math.min(MAX_PANEL_WIDTH, total - 360),
    )
  }

  const clampPanelWidth = (width: number) =>
    Math.min(maxPanelWidth(), Math.max(MIN_PANEL_WIDTH, width))

  const persistPanelWidth = (width: number) => {
    try {
      localStorage.setItem(PANEL_WIDTH_KEY, String(Math.round(width)))
    } catch {
      // 隐私模式写不了 localStorage：本次会话仍可拖动
    }
  }

  useEffect(() => {
    panelWidthRef.current = panelWidth
  }, [panelWidth])

  useEffect(() => {
    const onResize = () =>
      setPanelWidth((width) => clampPanelWidth(width))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!resizingPanel) return
    document.body.classList.add('col-resizing')

    const onMove = (event: PointerEvent) => {
      const drag = dragRef.current
      if (!drag) return
      const next = clampPanelWidth(
        drag.startWidth + (event.clientX - drag.startX) / drag.zoom,
      )
      panelWidthRef.current = next
      setPanelWidth(next)
    }
    const stop = () => {
      setResizingPanel(false)
      dragRef.current = null
      document.body.classList.remove('col-resizing')
      persistPanelWidth(panelWidthRef.current)
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
      document.body.classList.remove('col-resizing')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resizingPanel])

  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    const zoom =
      Number.parseFloat(
        getComputedStyle(document.documentElement).zoom || '1',
      ) || 1
    dragRef.current = {
      startX: event.clientX,
      startWidth: panelWidthRef.current,
      zoom,
    }
    setResizingPanel(true)
  }

  const resizeBy = (delta: number) => {
    const next = clampPanelWidth(panelWidthRef.current + delta)
    panelWidthRef.current = next
    setPanelWidth(next)
    persistPanelWidth(next)
  }

  const onResizerKey = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      resizeBy(-20)
    } else if (event.key === 'ArrowRight') {
      event.preventDefault()
      resizeBy(20)
    } else if (event.key === 'Home') {
      event.preventDefault()
      resizeBy(DEFAULT_PANEL_WIDTH - panelWidthRef.current)
    }
  }

  const resetPanelWidth = () => {
    const next = clampPanelWidth(DEFAULT_PANEL_WIDTH)
    panelWidthRef.current = next
    setPanelWidth(next)
    persistPanelWidth(next)
  }

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
      const cfg = readyModel(loadModelSettings(), 'answer')
      if (!cfg) return
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
          toPayload(cfg),
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
      const cfg = readyModel(loadModelSettings(), 'answer')
      if (!cfg) return
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
    if (!paper || pdfMode) return
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
  }, [paper, pdfMode])

  useEffect(() => {
    const el = scrollRef.current
    if (!el || !paper || pdfMode) return

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
    // 从 PDF 切回重排时 Reader 会重新挂载，必须重新绑定滚动监听
  }, [paper, pdfMode])

  const jump = (id: string) => {
    const page = pageById.current.get(id)
    if (pdfMode && page) {
      // 原版视图没有重排节点，改成滚到这一页
      jumpSeq.current += 1
      setPdfJump({ page, token: jumpSeq.current })
      return
    }
    const node = scrollRef.current?.querySelector<HTMLElement>(`#${CSS.escape(id)}`)
    node?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  // 切到原版 PDF 时把当前页码带过去；切回来时恢复重排阅读的滚动位置
  const togglePdf = () => {
    if (!canShowPdf) return
    const next = !pdfMode
    if (next) {
      savedScroll.current = scrollRef.current?.scrollTop ?? 0
      setPdfPage(visiblePage)
    }
    setPdfMode(next)
    const url = new URL(window.location.href)
    if (next) url.searchParams.set('view', 'pdf')
    else url.searchParams.delete('view')
    window.history.replaceState({}, '', url)
  }

  const togglePdfExpand = () => {
    const next = !pdfExpanded
    setPdfExpanded(next)
    if (next) setPdfWidth((w) => Math.max(w, 1200))
    const url = new URL(window.location.href)
    if (next) url.searchParams.set('full', '1')
    else url.searchParams.delete('full')
    window.history.replaceState({}, '', url)
  }

  // 原版视图里 Reader 是卸载状态，滚动位置存在内存里，切回来补上
  useEffect(() => {
    if (pdfMode) return
    const el = scrollRef.current
    if (!el || !savedScroll.current) return
    const top = savedScroll.current
    const timer = window.setTimeout(() => {
      el.scrollTop = top
    }, 60)
    return () => window.clearTimeout(timer)
  }, [pdfMode])

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
        onScale={(v) => {
          if (pdfMode) {
            // 原版视图下 ± 控制页面宽度
            const step = v > scale ? 120 : -120
            setPdfWidth((w) => Math.min(1200, Math.max(600, w + step)))
          } else {
            setScale(Math.min(1.35, Math.max(0.85, v)))
          }
        }}
        translated={translated}
        total={total}
        onOpenSettings={() => {
          setSettingsKind('answer')
          setSettingsOpen(true)
        }}
        configured={configured}
        onBack={onBack}
        hasTranslation={translated > 0}
        translating={translating}
        page={pdfMode ? pdfPage : visiblePage}
        canShowPdf={canShowPdf}
        pdfMode={pdfMode}
        onTogglePdf={togglePdf}
        pdfHref={pdfHref}
        pdfExpanded={pdfExpanded}
        onTogglePdfExpand={togglePdfExpand}
      />

      <div
        ref={workspaceRef}
        className={`workspace ${pdfMode && pdfExpanded ? 'is-pdf-expanded' : ''}`}
        style={{ '--panel-w': `${panelWidth}px` } as CSSProperties}
      >
        {!(pdfMode && pdfExpanded) && (
          <>
            <LeftPanel
              paperId={paperId}
              paper={paper}
              outline={outline}
              points={points}
              activeId={activeId}
              onJump={jump}
              onOpenSettings={(kind) => {
                setSettingsKind(kind ?? 'answer')
                setSettingsOpen(true)
              }}
              settingsVersion={settingsVersion}
            />
            <div
              className="panel-resizer"
              role="separator"
              aria-orientation="vertical"
              aria-label="拖动调整阅读助手宽度"
              tabIndex={0}
              title="拖动调整宽度，双击恢复默认"
              onPointerDown={startResize}
              onKeyDown={onResizerKey}
              onDoubleClick={resetPanelWidth}
            />
          </>
        )}
        {pdfMode ? (
          <PdfPages
            paperId={paperId}
            pageCount={paper.page_count}
            pageSizes={paper.page_sizes}
            initialPage={visiblePage}
            width={pdfWidth}
            jump={pdfJump}
            onPageChange={setPdfPage}
          />
        ) : (
          <Reader
            paper={paper}
            mode={mode}
            scale={scale}
            scrollRef={scrollRef}
            canTranslate={configured}
            onOpenSettings={() => {
              setSettingsKind('answer')
              setSettingsOpen(true)
            }}
            hint={
              !configured
                ? '未配置回答模型，翻译未开启。填写模型与密钥后，打开论文会自动翻译当前页和下一页。'
                : translating
                  ? '正在翻译当前页…'
                  : '当前页还没有译文，停在这里几秒会自动开始翻译。'
            }
          />
        )}
      </div>

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        initialKind={settingsKind}
        sampleImage={sampleImage}
        onSaved={(next) => {
          setConfigured(Boolean(readyModel(next, 'answer')))
          setSettingsVersion((v) => v + 1)
          requested.current.clear() // 换模型后允许重翻
        }}
      />
    </div>
  )
}
