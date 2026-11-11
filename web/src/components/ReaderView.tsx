import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import type { Block, Paper, ReaderContextItem, ViewMode } from '../types'
import { loadPaper } from '../lib/api'
import { outlineOf } from '../lib/blocks'
import { keyPoints } from '../lib/assistant'
import {
  addAnnotation,
  deleteAnnotation,
  extractHighlights,
  listAnnotations,
  listHighlights,
  saveProgress,
  translateStream,
  updateAnnotation,
  type Annotation,
  type ReadingHighlight,
} from '../lib/library'
import { mediaUrl } from '../lib/mobile'
import { canRunLocally, serverReachable } from '../lib/connectivity'
import { readOffline, saveOfflineBlocks } from '../lib/offlineStore'
import { translatePagesLocal } from '../lib/localTranslate'
import { mapRangeToOther, pairFits } from '../lib/align'
import type { AnnotationActions } from './Annotations'
import { loadTargetLang } from '../lib/translation'
import { loadAutoHighlights, loadDefaultReadingMode } from '../lib/readingPrefs'
import {
  loadModelSettings,
  readyModel,
  syncVisionConfig,
  toPayload,
  type ModelKind,
} from '../lib/settings'
import { TopBar } from './TopBar'
import { LeftPanel, PANEL_TABS, type TabKey } from './LeftPanel'
import { MobileDock } from './MobileDock'
import { Reader } from './Reader'
import { SettingsDialog } from './SettingsDialog'

// 原版 PDF 需要 pdf.js（体积不小）：只在切到 PDF 视图时才加载
const PdfPages = lazy(async () => {
  const mod = await import('./PdfPages')
  return { default: mod.PdfPages }
})

interface Props {
  paperId: string
  onBack: () => void
}

const PANEL_WIDTH_KEY = 'paperead.panelWidth'
const MIN_PANEL_WIDTH = 260
const MAX_PANEL_WIDTH = 640
const DEFAULT_PANEL_WIDTH = 344

function readInitialTab(): TabKey {
  const q = new URLSearchParams(window.location.search).get('tab')
  return (PANEL_TABS.some((t) => t.key === q) ? q : 'guide') as TabKey
}

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
  const [mode, setMode] = useState<ViewMode>(loadDefaultReadingMode)
  const [scale, setScale] = useState(1)
  const [activeId, setActiveId] = useState('')
  const [visiblePage, setVisiblePage] = useState(1)
  const [settledPage, setSettledPage] = useState(1)
  const [translating, setTranslating] = useState(false)
  // 本机副本：页图、PDF 直链，以及来源（StoryReader 导入的书没有 PDF）
  const [pageUrls, setPageUrls] = useState<Record<number, string>>({})
  const [localKind, setLocalKind] = useState<'none' | 'paper' | 'storyreader'>('none')
  const [localPdfUrl, setLocalPdfUrl] = useState<string | null>(null)
  // 示例数据只有 paper.json，没有 PDF 可看
  const pdfHref = localPdfUrl ?? mediaUrl(`/api/papers/${encodeURIComponent(paperId)}/file`)
  const canShowPdf = paperId !== 'demo' && localKind !== 'storyreader'
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
  const [annotations, setAnnotations] = useState<Annotation[]>([])
  const [highlights, setHighlights] = useState<ReadingHighlight[]>([])
  const [autoHighlights, setAutoHighlights] = useState(loadAutoHighlights)
  // 示例数据没有后端，标注/进度只在真实论文上启用
  const annotationsEnabled = paperId !== 'demo'
  const [panelWidth, setPanelWidth] = useState(readPanelWidth)
  const [panelTab, setPanelTab] = useState<TabKey>(readInitialTab)
  // 阅读页点图表/公式旁的小圆圈 → 加入左侧问答附录
  const [contextItems, setContextItems] = useState<ReaderContextItem[]>([])
  const contextIds = useMemo(
    () => new Set(contextItems.map((item) => item.id)),
    [contextItems],
  )
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
  const restored = useRef(false)
  const paperRef = useRef<Paper | null>(null)
  const pageById = useRef<Map<string, number>>(new Map())
  const requested = useRef<Set<number>>(new Set())
  const abortRef = useRef<AbortController | null>(null)
  const inFlight = useRef(false)
  const inFlightPages = useRef<number[]>([])
  const queued = useRef<number[] | null>(null)
  /** 已经提取过阅读重点的页（服务端也会去重，这里只减少请求） */
  const extractedPages = useRef<Set<number>>(new Set())
  const requestingHighlights = useRef<Set<number>>(new Set())

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

  // ── 图表/公式 → 问答附录 ───────────────────────────────
  const attachContext = (item: ReaderContextItem) => {
    setContextItems((prev) =>
      prev.some((one) => one.id === item.id) ? prev : [...prev, item].slice(-6),
    )
    setPanelTab('chat')
  }
  const removeContext = (id: string) =>
    setContextItems((prev) => prev.filter((one) => one.id !== id))
  const clearContext = () => setContextItems([])

  useEffect(() => {
    setContextItems([])
  }, [paperId])

  // ── 加载（解析没完成时自动重试） ───────────────────────
  useEffect(() => {
    let alive = true
    let timer: number | undefined
    setPaper(null)
    setError('')
    setVisiblePage(1)
    setSettledPage(1)
    setPageUrls({})
    setLocalKind('none')
    setLocalPdfUrl(null)
    requested.current.clear()

    const attempt = async (n: number) => {
      // 下载到本机的副本优先：离开电脑后也能直接读电脑解析好的 blocks
      const local = await readOffline(paperId).catch(() => null)
      if (!alive) return
      if (local) {
        setParsing(false)
        setPageUrls(local.pageUrls)
        setLocalPdfUrl(local.pdfUrl)
        setLocalKind(local.source)
        setPaper(local.paper)
        return
      }
      try {
        const p = await loadPaper(paperId)
        if (!alive) return
        setParsing(false)
        setPaper(p)
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        if (!alive) return
        // 409：还在解析中（版面 + 图表 + 公式识别），等一会儿再试（最多约 4 分钟）
        if (msg.includes('解析') && n < 160) {
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

  // StoryReader 导入的书没有 PDF，误带 ?view=pdf 时回到重排阅读
  useEffect(() => {
    if (localKind === 'storyreader' && pdfMode) setPdfMode(false)
  }, [localKind, pdfMode])

  useEffect(() => {
    paperRef.current = paper
    if (!paper) return
    const map = new Map<string, number>()
    paper.blocks.forEach((b) => map.set(b.id, b.page))
    pageById.current = map
  }, [paper])

  // ── 高亮 / 笔记 ────────────────────────────────────────
  useEffect(() => {
    if (!annotationsEnabled) {
      setAnnotations([])
      return
    }
    let alive = true
    listAnnotations(paperId)
      .then((rows) => {
        if (alive) setAnnotations(rows)
      })
      .catch(() => {
        if (alive) setAnnotations([])
      })
    return () => {
      alive = false
    }
  }, [paperId, annotationsEnabled])

  // 设置里切换「自动阅读重点」：立即生效，不用刷新
  useEffect(() => {
    const onChanged = (event: Event) => {
      setAutoHighlights(Boolean((event as CustomEvent).detail))
    }
    window.addEventListener('paperead:auto-highlights-changed', onChanged)
    return () =>
      window.removeEventListener('paperead:auto-highlights-changed', onChanged)
  }, [])

  // 设置里切换「默认阅读模式」：当前论文立即跟着换视图
  useEffect(() => {
    const onChanged = (event: Event) => {
      setMode((event as CustomEvent<ViewMode>).detail)
    }
    window.addEventListener('paperead:reading-mode-changed', onChanged)
    return () =>
      window.removeEventListener('paperead:reading-mode-changed', onChanged)
  }, [])

  const mergeHighlights = useCallback((page: number, items: ReadingHighlight[]) => {
    setHighlights((prev) => [...prev.filter((item) => item.page !== page), ...items])
    extractedPages.current.add(page)
  }, [])

  // ── 自动阅读重点（研究目标 / 方法 / 结论） ──────────────
  useEffect(() => {
    if (!annotationsEnabled) {
      setHighlights([])
      extractedPages.current = new Set()
      return
    }
    let alive = true
    listHighlights(paperId)
      .then((data) => {
        if (!alive) return
        setHighlights(data.highlights ?? [])
        extractedPages.current = new Set(
          Object.keys(data.pages ?? {}).map((page) => Number(page)),
        )
      })
      .catch(() => {
        if (alive) setHighlights([])
      })
    return () => {
      alive = false
    }
  }, [paperId, annotationsEnabled])

  const annotationActions = useMemo<AnnotationActions | undefined>(() => {
    if (!annotationsEnabled) return undefined
    return {
      create: async (input) => {
        const row = await addAnnotation(paperId, input)
        setAnnotations((prev) => [...prev, row])
        return row
      },
      update: async (id, patch) => {
        const row = await updateAnnotation(paperId, id, patch)
        setAnnotations((prev) => prev.map((item) => (item.id === row.id ? row : item)))
      },
      remove: async (id) => {
        await deleteAnnotation(paperId, id)
        setAnnotations((prev) => prev.filter((item) => item.id !== id))
      },
    }
  }, [paperId, annotationsEnabled])

  // 旧标注、或在译文还没出来时划的荧光笔：等译文就绪后补上另一侧片段，
  // 这样原文高亮会自动出现在译文里；重译后对不上的也会重算一次。
  const pairAttempts = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (!paper || !annotationsEnabled || !annotationActions) return
    const blocks = new Map(paper.blocks.map((b) => [b.id, b]))
    for (const item of annotations) {
      const block = blocks.get(item.block_id)
      if (!block) continue
      const ownText = item.source === 'en' ? block.text : (block.translated ?? '')
      const otherText = item.source === 'en' ? (block.translated ?? '') : block.text
      if (!ownText || !otherText) continue
      // 已有对侧片段且合得上就跳过
      if (item.pair?.source !== item.source && pairFits(otherText, item.pair)) continue
      const attempt = `${item.id}:${ownText.length}:${otherText.length}`
      if (pairAttempts.current.has(attempt)) continue
      pairAttempts.current.add(attempt)
      const pair = mapRangeToOther(ownText, otherText, item.start, item.end, item.source)
      if (pair) {
        void annotationActions.update(item.id, { pair }).catch(() => {
          // 段落刚好在重译、文本变短时后端会拒绝；下一轮文本长度变了再试
        })
      }
    }
  }, [paper, annotations, annotationsEnabled, annotationActions])

  // 设置里切换译文语言：清掉本地旧译文，交给翻译流程按新语言重来
  useEffect(() => {
    if (paperId === 'demo') return
    const onChanged = () => {
      requested.current.clear()
      abortRef.current?.abort()
      const lang = loadTargetLang()
      setHighlights([])
      extractedPages.current = new Set()
      requestingHighlights.current.clear()
      setPaper((prev) =>
        prev
          ? {
              ...prev,
              translation_lang: lang,
              glossary: {},
              blocks: prev.blocks.map((b) => ({ ...b, translated: '' })),
            }
          : prev,
      )
    }
    window.addEventListener('paperead:translation-changed', onChanged)
    return () =>
      window.removeEventListener('paperead:translation-changed', onChanged)
  }, [paperId])

  // 打开论文时如果库里的译文语言和当前设置不一致，先清掉旧译文再按新语言翻。
  // 空值表示旧版本数据（默认中文），不能因此误清。
  useEffect(() => {
    if (!paper || paperId === 'demo') return
    const lang = loadTargetLang()
    if ((paper.translation_lang || 'zh-Hans') === lang) return
    setHighlights([])
    extractedPages.current = new Set()
    requestingHighlights.current.clear()
    setPaper((prev) =>
      prev
        ? {
            ...prev,
            translation_lang: lang,
            glossary: {},
            blocks: prev.blocks.map((b) => ({ ...b, translated: '' })),
          }
        : prev,
    )
  }, [paper, paperId])

  // ── 阅读进度 ───────────────────────────────────────────
  // 打开论文时回到上次位置；深链优先，避免覆盖用户主动跳转
  useEffect(() => {
    if (!paper || pdfMode || !annotationsEnabled || restored.current) return
    restored.current = true
    const hash = decodeURIComponent(window.location.hash.slice(1))
    const progress = paper.progress
    if (hash || !progress || (!progress.block_id && !progress.pct)) return
    // 一次性动作：论文对象会随翻译流更新，这里不能把定时器绑在依赖清理上
    window.setTimeout(() => {
      const el = scrollRef.current
      if (!el) return
      const node = progress.block_id
        ? el.querySelector<HTMLElement>(`#${CSS.escape(progress.block_id)}`)
        : null
      if (node) {
        node.scrollIntoView({ block: 'start' })
      } else {
        el.scrollTop = (progress.pct / 100) * Math.max(0, el.scrollHeight - el.clientHeight)
      }
    }, 120)
  }, [paper, pdfMode, annotationsEnabled])

  // 滚动停稳后低频上报，避免每帧写盘
  const readerReady = Boolean(paper)
  useEffect(() => {
    if (!readerReady || pdfMode || !annotationsEnabled) return
    const el = scrollRef.current
    if (!el) return
    let timer: number | undefined
    const report = () => {
      const max = Math.max(0, el.scrollHeight - el.clientHeight)
      const pct = max ? Math.min(100, Math.max(0, (el.scrollTop / max) * 100)) : 0
      const threshold = el.getBoundingClientRect().top + 120
      let blockId = ''
      el.querySelectorAll<HTMLElement>('[data-block-id]').forEach((node) => {
        if (node.getBoundingClientRect().top <= threshold) {
          blockId = node.dataset.blockId || blockId
        }
      })
      void saveProgress(paperId, {
        pct: Math.round(pct * 10) / 10,
        block_id: blockId,
        page: pageById.current.get(blockId) ?? 1,
      }).catch(() => {})
    }
    const onScroll = () => {
      if (timer) window.clearTimeout(timer)
      timer = window.setTimeout(report, 1200)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      el.removeEventListener('scroll', onScroll)
      if (timer) window.clearTimeout(timer)
    }
  }, [readerReady, pdfMode, annotationsEnabled, paperId])

  // ── 按页懒翻译：单飞 + 看门狗重试（流式，逐段出现） ──────
  const ensurePages = useCallback(
    async (pages: number[]) => {
      const current = paperRef.current
      if (!current || paperId === 'demo') return
      const cfg = readyModel(loadModelSettings(), 'answer')
      if (!cfg) return
      const targetLang = loadTargetLang()
      // 同一时刻只跑一条流。用户快速滑动时：若正在翻的页已经离开视野，
      // 直接中止它，把配额让给用户真正停下来的那一页；否则排到队尾。
      if (inFlight.current) {
        if (!inFlightPages.current.includes(pages[0])) {
          abortRef.current?.abort()
        }
        queued.current = pages
        return
      }

      const sameLang = (current.translation_lang || 'zh-Hans') === targetLang
      const need = pages.filter((pg) => {
        if (requested.current.has(pg)) return false
        const blocks = current.blocks.filter(
          (b) => b.page === pg && b.translatable && b.text.trim(),
        )
        if (!blocks.length) return false
        // 目标语言变了：即使本地已有旧译文，也要重新请求
        return !sameLang || blocks.some((b) => !b.translated)
      })
      if (!need.length) return

      // 服务器不可达时走本机：模型请求由 Rust 发出，逐段流式回填
      const online = await serverReachable()
      if (!online && !canRunLocally()) return

      need.forEach((pg) => requested.current.add(pg))
      inFlight.current = true
      inFlightPages.current = need
      const ac = new AbortController()
      abortRef.current = ac
      setTranslating(true)
      try {
        if (!online) {
          // 本地状态可能还没提交到 paperRef，单独留一份，保证写库不丢译文
          const translated = new Map<string, string>()
          const apply = (blockId: string, text: string) => {
            translated.set(blockId, text)
            setPaper((prev) =>
              prev
                ? {
                    ...prev,
                    translation_lang: targetLang,
                    blocks: prev.blocks.map((b) =>
                      b.id === blockId ? { ...b, translated: text } : b,
                    ),
                  }
                : prev,
            )
          }
          await translatePagesLocal({
            paper: current,
            pages: need,
            config: { baseUrl: cfg.baseUrl, apiKey: cfg.apiKey, model: cfg.model },
            targetLang,
            signal: ac.signal,
            onPartial: apply,
            onBlock: apply,
            onPageDone: async () => {
              const snapshot = paperRef.current ?? current
              const blocks = snapshot.blocks.map((b) =>
                translated.has(b.id)
                  ? { ...b, translated: translated.get(b.id) as string }
                  : b,
              )
              await saveOfflineBlocks(paperId, blocks, targetLang).catch(() => undefined)
            },
          })
          return
        }
        await translateStream(
          paperId,
          toPayload(cfg),
          need,
          (evt) => {
            if (evt.type === 'reset') {
              setHighlights([])
              extractedPages.current = new Set()
              requestingHighlights.current.clear()
              setPaper((prev) =>
                prev
                  ? {
                      ...prev,
                      translation_lang: evt.target_lang ?? loadTargetLang(),
                      glossary: {},
                      blocks: prev.blocks.map((b) => ({ ...b, translated: '' })),
                    }
                  : prev,
              )
            }
            if (evt.type === 'highlights' && evt.page) {
              mergeHighlights(evt.page, evt.highlights ?? [])
            }
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
          targetLang,
          autoHighlights,
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
    [paperId, mergeHighlights, autoHighlights],
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

  // 翻译完成后补提取阅读重点：新翻的页由翻译流直接带回，
  // 这里兜底处理「打开时已经翻好」的旧论文。服务端会跳过已处理页。
  useEffect(() => {
    if (paperId === 'demo' || !configured || !autoHighlights) return
    const run = () => {
      const current = paperRef.current
      if (!current) return
      const cfg = readyModel(loadModelSettings(), 'answer')
      if (!cfg) return
      const maxPage = current.page_count || 1
      const pages = [settledPage, settledPage + 1].filter((p) => p >= 1 && p <= maxPage)
      const need = pages.filter(
        (pg) =>
          !extractedPages.current.has(pg) &&
          !requestingHighlights.current.has(pg) &&
          current.blocks.some(
            (b) => b.page === pg && b.translatable && b.text.trim() && b.translated,
          ),
      )
      if (!need.length) return
      need.forEach((pg) => requestingHighlights.current.add(pg))
      void extractHighlights(paperId, toPayload(cfg), need)
        .then((res) => {
          const processed = new Set(res.processed ?? [])
          processed.forEach((pg) =>
            mergeHighlights(
              pg,
              (res.highlights ?? []).filter((item) => item.page === pg),
            ),
          )
          // 服务端已经处理过的页也记下来，避免每 4 秒重复请求
          Object.keys(res.pages ?? {}).forEach((page) =>
            extractedPages.current.add(Number(page)),
          )
        })
        .catch(() => {
          // 失败不记账，下一轮重试
        })
        .finally(() => {
          need.forEach((pg) => requestingHighlights.current.delete(pg))
        })
    }
    const first = window.setTimeout(run, 1500)
    const timer = window.setInterval(run, 4000)
    return () => {
      window.clearTimeout(first)
      window.clearInterval(timer)
    }
  }, [paperId, settledPage, configured, mergeHighlights, autoHighlights])

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

  const jump = (id: string, page?: number) => {
    if (page) {
      // 批注栏里的 PDF 标注：即使当前在重排视图也切回原版并滚到该页
      if (!pdfMode) {
        savedScroll.current = scrollRef.current?.scrollTop ?? 0
        setPdfMode(true)
        const url = new URL(window.location.href)
        url.searchParams.set('view', 'pdf')
        window.history.replaceState({}, '', url)
      }
      jumpSeq.current += 1
      setPdfJump({ page, token: jumpSeq.current })
      return
    }
    const targetPage = pageById.current.get(id)
    if (pdfMode && targetPage) {
      // 原版视图没有重排节点，改成滚到这一页
      jumpSeq.current += 1
      setPdfJump({ page: targetPage, token: jumpSeq.current })
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
              annotations={annotations}
              onUpdateAnnotation={annotationActions?.update ?? (async () => {})}
              onDeleteAnnotation={annotationActions?.remove ?? (async () => {})}
              highlights={highlights}
              tab={panelTab}
              onTab={setPanelTab}
              contextItems={contextItems}
              onRemoveContext={removeContext}
              onClearContext={clearContext}
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
          <Suspense
            fallback={
              <div className="pdf-view">
                <div className="pdf-pages">
                  <p className="pdf-page-hint">正在准备原版 PDF…</p>
                </div>
              </div>
            }
          >
            <PdfPages
              paperId={paperId}
              pageCount={paper.page_count}
              pageSizes={paper.page_sizes}
              initialPage={visiblePage}
              width={pdfWidth}
              jump={pdfJump}
              onPageChange={setPdfPage}
              pageUrls={pageUrls}
              pdfUrl={pdfHref}
              annotations={annotations}
              annotationActions={annotationActions}
            />
          </Suspense>
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
            annotations={annotations}
            annotationActions={annotationActions}
            highlights={highlights}
            contextIds={contextIds}
            onAttachContext={attachContext}
          />
        )}
      </div>

      {!(pdfMode && pdfExpanded) && (
        <MobileDock
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
          annotations={annotations}
          onUpdateAnnotation={annotationActions?.update ?? (async () => {})}
          onDeleteAnnotation={annotationActions?.remove ?? (async () => {})}
          highlights={highlights}
          contextItems={contextItems}
          onRemoveContext={removeContext}
          onClearContext={clearContext}
        />
      )}

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        initialKind={settingsKind}
        sampleImage={sampleImage}
        onSaved={(next) => {
          setConfigured(Boolean(readyModel(next, 'answer')))
          setSettingsVersion((v) => v + 1)
          requested.current.clear() // 换模型后允许重翻
          void syncVisionConfig() // 识图模型变了：同步给服务端，重新解析时生效
        }}
      />
    </div>
  )
}
