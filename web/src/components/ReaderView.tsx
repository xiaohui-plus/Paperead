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
import type { Block, Paper, ReaderContextItem, ReaderQuote, ReaderQuoteDraft, ViewMode } from '../types'
import { loadPaper } from '../lib/api'
import { outlineOf } from '../lib/blocks'
import { keyPoints } from '../lib/assistant'
import {
  addAnnotation,
  ApiError,
  deleteAnnotation,
  extractHighlights,
  fetchTranslationManifest,
  getActiveTranslationJob,
  getEngineStatus,
  getTranslationJob,
  listAnnotations,
  listHighlights,
  probeModel,
  recognizePaperFormulas,
  resumeTranslationJob,
  saveProgress,
  startTranslationJob,
  translateStream,
  updateAnnotation,
  type Annotation,
  type ReadingHighlight,
  type TranslationJob,
  type TranslationManifest,
  type TranslationReport,
} from '../lib/library'
import { mediaUrl } from '../lib/mobile'
import { canRunLocally, serverReachable } from '../lib/connectivity'
import { readOffline, saveOfflineBlocks } from '../lib/offlineStore'
import { translatePagesLocal } from '../lib/localTranslate'
import { priorityBlocks } from '../lib/translationPriority'
import { mapRangeToOther, pairFits } from '../lib/align'
import type { AnnotationActions } from './Annotations'
import { loadTargetLang } from '../lib/translation'
import {
  detectPaperLanguage,
  defaultAutoTranslateForPaper,
  sameLanguage,
  PAPER_LANG_LABELS,
  type PaperLang,
} from '../lib/paperLang'
import { loadAutoHighlights, loadAutoTranslate, loadDefaultReadingMode, loadPaperAutoTranslate, saveAutoHighlights, savePaperAutoTranslate } from '../lib/readingPrefs'
import {
  loadModelSettings,
  readyModel,
  syncVisionConfig,
  toPayload,
  visionModel,
  type ModelKind,
} from '../lib/settings'
import { TopBar } from './TopBar'
import { IconBack } from './icons'
import { LeftPanel, PANEL_TABS, type TabKey } from './LeftPanel'
import { MobileDock } from './MobileDock'
import { Reader } from './Reader'
import { DualReader } from './DualReader'
import { EngineDownloadDialog } from './EngineDownloadDialog'
import { TranslationSkeleton } from './TranslationSkeleton'
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
// 窗口压到这么窄时自动收起左侧阅读栏，把宽度还给正文；拉宽后自动恢复。
// 阅读页整体带 120% 缩放，这里比较的是折算后的布局宽度。
const AUTO_COLLAPSE_WIDTH = 980

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

const PANEL_COLLAPSED_KEY = 'paperead.panelCollapsed'
const PDF_REVIEW_KEY = 'paperead.pdfReview'

function readPanelCollapsed(): boolean {
  try {
    return localStorage.getItem(PANEL_COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

/** PDF 审阅模式开关；关掉后原版视图恢复原来的单页居中布局。 */
function readPdfReview(): boolean {
  try {
    return localStorage.getItem(PDF_REVIEW_KEY) === '1'
  } catch {
    return false
  }
}

/** 阅读页的布局宽度：window.innerWidth 是视口宽，需按 ui-zoom 折算。 */
function readingLayoutWidth(): number {
  const zoom =
    Number.parseFloat(
      getComputedStyle(document.documentElement).zoom || '1',
    ) || 1
  return (document.documentElement.clientWidth || window.innerWidth) / zoom
}

/** 页面放大倍率：1 = 适配宽度，上限 3，按 1% 取整，滚轮和按钮共用。 */
function clampZoom(value: number): number {
  return Math.min(3, Math.max(1, Math.round(value * 100) / 100))
}

/**
 * 外部翻译引擎的阶段文案。
 *
 * PDF 全文翻译由 BabelDOC 完成，它按自己的阶段和段落推进；应用侧回填的
 * block 「已翻译段数」只是映射估算，不能当作真实计数，因此有阶段信息时
 * 一律展示引擎自己的进度，只有拿不到阶段（离线分块翻译）才回退到块数。
 */
function engineStageText(
  stage: string | undefined,
  stageDone: number | undefined,
  stageTotal: number | undefined,
  fallback: string,
): string {
  switch (stage) {
    case 'parse':
      return '正在解析版面'
    case 'translate':
      return stageTotal
        ? `正在翻译段落 ${stageDone ?? 0}/${stageTotal}`
        : '正在翻译段落'
    case 'typeset':
      return '正在排版译文'
    case 'save':
      return '正在保存译文 PDF'
    case 'done':
      return '正在回填译文'
    default:
      return fallback
  }
}

export function ReaderView({ paperId, onBack }: Props) {
  const [paper, setPaper] = useState<Paper | null>(null)
  const [error, setError] = useState('')
  const [parsing, setParsing] = useState(false)
  const [mode, setMode] = useState<ViewMode>(loadDefaultReadingMode)
  const [scale, setScale] = useState(1)
  const [activeId, setActiveId] = useState('')
  const [visiblePage, setVisiblePage] = useState(1)
  const [visibleBlocks, setVisibleBlocks] = useState<string[]>([])
  const [translationRetry, setTranslationRetry] = useState(0)
  const [translationFailures, setTranslationFailures] = useState<Record<string, number>>({})
  // 可译清单与验收报告：全文翻译的 pending / 失败 / 覆盖率都以服务端为准
  const [translationManifest, setTranslationManifest] = useState<TranslationManifest | null>(null)
  // 服务端任务进度：段数与进度条只认它。本地 blocks 里的译文还混着阅读流
  // 按页翻译写回的块（不可译块也可能带译文），拿它当任务进度会虚高。
  // PDF 全文翻译走外部引擎，块数只是映射估计值；引擎自己上报的 stage 才是准确口径。
  const [jobProgress, setJobProgress] = useState<{
    done: number
    total: number
    failed: number
    stage?: string
    stageDone?: number
    stageTotal?: number
  } | null>(null)
  const [translationNotice, setTranslationNotice] = useState<{
    tone: 'info' | 'warn' | 'error'
    text: string
    action?: 'retry' | 'settings' | 'resume'
  } | null>(null)
  // 整篇翻译引擎未就绪时打开下载弹窗（带进度），下载完成后自动继续翻译
  const [engineDialogOpen, setEngineDialogOpen] = useState(false)
  const manifestStatus = useMemo(
    () =>
      new Map(
        (translationManifest?.blocks ?? []).map((row) => [row.id, row.status] as const),
      ),
    [translationManifest],
  )
  const [settledPage, setSettledPage] = useState(1)
  const [translating, setTranslating] = useState(false)
  // 全文翻译刚点击、清单与任务还没落地的瞬间：骨架直接进「翻译中」而不是「未开始」
  const [translateStarting, setTranslateStarting] = useState(false)
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
  // 全文翻译双栏：左原 PDF、右按块流式译文；与 pdfMode 互斥
  const [dualMode, setDualMode] = useState(
    () =>
      paperId !== 'demo' &&
      new URLSearchParams(window.location.search).get('view') === 'dual',
  )
  // 双栏模式临时折叠阅读助手：只影响本次模式，不写入用户偏好
  const [dualCollapsed, setDualCollapsed] = useState(
    () =>
      paperId !== 'demo' &&
      new URLSearchParams(window.location.search).get('view') === 'dual',
  )
  // 原版 PDF 的显示宽度与当前页码；页码跟着 PDF 滚动更新，顶栏显示 P{n}
  const initialExpanded =
    new URLSearchParams(window.location.search).get('full') === '1'
  const [pdfWidth, setPdfWidth] = useState(initialExpanded ? 1200 : 1000)
  const [pdfPage, setPdfPage] = useState(1)
  // 右栏译文 PDF 的起始页：只在进入双栏那一刻取一次，之后完全交给滚动联动
  const [dualStartPage, setDualStartPage] = useState(1)
  // 默认只占右侧阅读区、保留左侧功能面板；展开后才占满整行
  const [pdfExpanded, setPdfExpanded] = useState(initialExpanded)
  // 审阅模式：在 PDF 页面右侧按位置渲染每条批注
  const [pdfReview, setPdfReview] = useState(readPdfReview)
  // 窄屏没有放右侧批注栏的位置：审阅模式改由底部「批注」抽屉承载
  const [narrowLayout, setNarrowLayout] = useState(
    () =>
      typeof window !== 'undefined' &&
      window.matchMedia('(max-width: 860px)').matches,
  )

  useEffect(() => {
    const mq = window.matchMedia('(max-width: 860px)')
    const onChange = () => setNarrowLayout(mq.matches)
    onChange()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
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
  const [formulaRepairing, setFormulaRepairing] = useState(false)
  const formulaRepairTried = useRef<Set<string>>(new Set())
  const [annotations, setAnnotations] = useState<Annotation[]>([])
  const [highlights, setHighlights] = useState<ReadingHighlight[]>([])
  const [autoHighlights, setAutoHighlights] = useState(loadAutoHighlights)
  const [autoTranslate, setAutoTranslate] = useState(loadAutoTranslate)
  // 当前目标译文语种：与原文语种一致时默认不自动翻译
  const [targetLang, setTargetLang] = useState(loadTargetLang)
  const [translatedPdfReady, setTranslatedPdfReady] = useState(false)
  const [translatedPdfVersion, setTranslatedPdfVersion] = useState(0)
  const [pdfBuilding, setPdfBuilding] = useState(false)
  // 双栏：左原文栏默认显示、滚动默认联动；关掉原文后译文占满
  const [dualShowSource, setDualShowSource] = useState(true)
  const [dualSync, setDualSync] = useState(true)
  // 双栏页面放大倍率：1 = 适配列宽；放大后可横向滚动查看页面局部，两栏视野保持同步
  const [dualZoom, setDualZoom] = useState(1)
  // 原版 PDF 的放大倍率与手型工具：Ctrl+滚轮改倍率，手型工具下可拖动平移
  const [pdfZoom, setPdfZoom] = useState(1)
  const [pdfPan, setPdfPan] = useState(false)
  // 译文 PDF：P2 生成的页级回填产物，右栏直接用 pdf.js 渲染它
  const translatedPdfUrl =
    paperId !== 'demo'
      ? `/api/papers/${encodeURIComponent(paperId)}/translated-pdf?lang=${encodeURIComponent(targetLang)}&v=${translatedPdfVersion}`
      : ''
  // 当前论文原文语种：中文文献打开时默认关掉自动翻译
  const [paperLang, setPaperLang] = useState<PaperLang>('unknown')
  // 示例数据没有后端，标注/进度只在真实论文上启用
  const annotationsEnabled = paperId !== 'demo'
  const [panelWidth, setPanelWidth] = useState(readPanelWidth)
  const [panelCollapsed, setPanelCollapsed] = useState(readPanelCollapsed)
  // 窗口太窄时自动折叠侧栏：只影响本次窗口尺寸，不覆盖用户手动折叠偏好
  const [autoCollapsed, setAutoCollapsed] = useState(
    () =>
      typeof window !== 'undefined' &&
      readingLayoutWidth() < AUTO_COLLAPSE_WIDTH,
  )
  // 用户在窄屏手动展开过：直到窗口重新变宽前都不再自动收起
  const narrowPanelOverride = useRef(false)
  const panelHidden = panelCollapsed || autoCollapsed || dualCollapsed
  const [panelTab, setPanelTab] = useState<TabKey>(readInitialTab)
  // 双击正文高亮后要聚焦的批注条：at 让连续双击同一条也能重新滚动
  const [focusAnnotation, setFocusAnnotation] = useState<{
    id: string
    at: number
  } | null>(null)
  // 双击自动阅读重点后要聚焦的条目
  const [focusHighlight, setFocusHighlight] = useState<{
    id: string
    at: number
  } | null>(null)
  // 阅读页点图表/公式旁的小圆圈 → 加入左侧问答附录
  const [contextItems, setContextItems] = useState<ReaderContextItem[]>([])
  const contextIds = useMemo(
    () => new Set(contextItems.map((item) => item.id)),
    [contextItems],
  )
  // 划词点「提问」→ 引用片段带到左侧问答面板
  const [quotes, setQuotes] = useState<ReaderQuote[]>([])
  // at 变化时问答面板聚焦输入框（连续划词也能重新聚焦）
  const [quoteFocus, setQuoteFocus] = useState<{ at: number } | null>(null)
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
  // 全文翻译与按页翻译分开中止：滚动换页只中止按页流，不能把整篇任务打断。
  const fullAbortRef = useRef<AbortController | null>(null)
  const fullRunning = useRef(false)
  // 轮询 token：换论文/卸载时只让前端停止轮询，服务端任务继续跑。
  const pollTokenRef = useRef(0)
  const inFlight = useRef(false)
  const inFlightPages = useRef<number[]>([])
  const inFlightBlocks = useRef<string[]>([])
  const queued = useRef<{ pages: number[]; ids?: string[] } | null>(null)
  // 供异步的翻译流读取最新开关：await 期间用户关掉自动翻译时，
  // 旧闭包里的 autoTranslate 仍是 true，必须用 ref 复检才能拦住。
  const autoTranslateRef = useRef(autoTranslate)
  /** 已经提取过阅读重点的页（服务端也会去重，这里只减少请求） */
  const extractedPages = useRef<Set<number>>(new Set())
  const requestingHighlights = useRef<Set<number>>(new Set())

  /** 切换当前论文的自动翻译：关闭时立刻中止正在跑的翻译流 */
  const applyAutoTranslate = useCallback((enabled: boolean) => {
    setAutoTranslate(enabled)
    autoTranslateRef.current = enabled
    if (!enabled) {
      abortRef.current?.abort()
      abortRef.current = null
      fullAbortRef.current?.abort()
      fullRunning.current = false
      pollTokenRef.current += 1
      queued.current = null
      // 关闭后重新打开时要能重新请求；同时立刻收起「翻译中」状态，
      // 不等被 abort 的流在自己的 finally 里才复位。
      requested.current.clear()
      setTranslating(false)
    }
  }, [])

  useEffect(() => {
    autoTranslateRef.current = autoTranslate
  }, [autoTranslate])

  /** 按论文语种给出默认值：与目标译文语种一致就关闭，不一致才自动翻译（尊重总开关） */
  const adoptPaperLanguage = useCallback((next: Paper) => {
    const lang = detectPaperLanguage(next)
    const target = loadTargetLang()
    setPaperLang(lang)
    setTargetLang(target)
    const manual = loadPaperAutoTranslate(paperId)
    applyAutoTranslate(
      manual ?? defaultAutoTranslateForPaper(lang, loadAutoTranslate(), target),
    )
  }, [paperId, applyAutoTranslate])

  /** 顶栏手动切换：只记这篇论文，不改全局总开关 */
  const toggleAutoTranslate = useCallback(() => {
    const next = !autoTranslate
    applyAutoTranslate(next)
    if (paperId !== 'demo') savePaperAutoTranslate(paperId, next)
  }, [autoTranslate, applyAutoTranslate, paperId])

  /** 拉取服务端可译清单；失败不打断阅读，沿用上一次结果。 */
  const refreshTranslationManifest = useCallback(async () => {
    if (paperId === 'demo') return null
    try {
      const manifest = await fetchTranslationManifest(paperId, loadTargetLang())
      setTranslationManifest(manifest)
      return manifest
    } catch {
      return null
    }
  }, [paperId])

  useEffect(() => {
    if (paperId === 'demo') return
    void refreshTranslationManifest()
  }, [paperId, targetLang, refreshTranslationManifest])

  /** 确认译文 PDF 是否已由全局翻译引擎产出；缺失时服务端会要求重新发起全文翻译 */
  const buildTranslatedPdf = useCallback(async () => {
    if (paperId === 'demo') return
    setPdfBuilding(true)
    try {
      const res = await fetch(
        `/api/papers/${encodeURIComponent(paperId)}/translated-pdf/build?lang=${encodeURIComponent(targetLang)}`,
        { method: 'POST' },
      )
      const data = (await res.json().catch(() => ({}))) as {
        cached?: boolean
        status?: string
        coverage?: number
        detail?: string
      }
      if (!res.ok) {
        // 依赖（翻译引擎）还没下载好：这不是翻译失败，打开带进度的下载弹窗，
        // 装好后会自动继续，避免只给一行「译文 PDF 生成失败」。
        if (res.headers.get('X-Translation-Code') === 'engine_missing') {
          setTranslationNotice(null)
          setEngineDialogOpen(true)
          return
        }
        setTranslationNotice({
          tone: 'error',
          text: data.detail || `译文 PDF 生成失败（HTTP ${res.status}）`,
        })
        return
      }
      if (!data.cached) setTranslatedPdfVersion((v) => v + 1)
      if (data.status === 'partial') {
        const pct = Math.round((data.coverage ?? 0) * 100)
        // 翻译流本身已经报错时，不要把「为什么失败」覆盖成覆盖率提示。
        setTranslationNotice((prev) =>
          prev?.tone === 'error'
            ? prev
            : {
                tone: 'warn',
                text: `译文 PDF 已生成，但正文覆盖率只有 ${pct}%，未翻部分保留原文。`,
                action: 'retry',
              },
        )
      }
    } catch (e) {
      setTranslationNotice({
        tone: 'error',
        text: e instanceof Error ? e.message : '译文 PDF 生成失败',
      })
    } finally {
      setPdfBuilding(false)
    }
  }, [paperId, targetLang])

  /** 把服务端任务返回的新块合并进 paper；轮询与重连共用。 */
  const applyTranslationBlocks = useCallback(
    (blocks: Record<string, unknown>[], lang: string) => {
      if (!blocks.length) return
      const updates = new Map(
        blocks.map((item) => [
          String((item as { id?: string }).id),
          item as unknown as Block,
        ]),
      )
      setPaper((prev) =>
        prev
          ? {
              ...prev,
              translation_lang: lang,
              blocks: prev.blocks.map((b) => updates.get(b.id) ?? b),
            }
          : prev,
      )
    },
    [],
  )

  /**
   * 轮询服务端任务。组件卸载/切论文只停止轮询，不取消服务端任务；
   * 重新进入论文时由 active-job 副作用自动接上。
   */
  const pollTranslationJob = useCallback(
    async (job: TranslationJob, cursor = 0) => {
      const token = ++pollTokenRef.current
      fullRunning.current = true
      setTranslateStarting(false)
      setTranslating(true)
      setDualMode(true)
      setDualCollapsed(true)
      setPdfMode(false)
      setJobProgress({
        done: job.done,
        total: job.total,
        failed: job.failed,
        stage: job.stage,
        stageDone: job.stage_done,
        stageTotal: job.stage_total,
      })

      let jobCursor = cursor
      let report: TranslationReport | null = job.report ?? null
      let terminal: TranslationJob['status'] = job.status
      let lastError: { code: string; message: string } | null = job.error ?? null


      try {
        while (pollTokenRef.current === token) {
          const data = await getTranslationJob(paperId, job.job_id, jobCursor)
          if (pollTokenRef.current !== token) return
          applyTranslationBlocks(data.blocks ?? [], data.lang)
          jobCursor = data.next_cursor ?? jobCursor
          terminal = data.status
          lastError = data.error ?? null
          report = data.report ?? report
          setJobProgress({
            done: data.done,
            total: data.total,
            failed: data.failed,
            stage: data.stage,
            stageDone: data.stage_done,
            stageTotal: data.stage_total,
          })

          if (terminal === 'running' || terminal === 'queued') {
            setTranslationNotice({
              tone: 'info',
              text: engineStageText(data.stage, data.stage_done, data.stage_total, '正在准备翻译任务…'),
            })

            await new Promise((resolve) =>
              window.setTimeout(resolve, 1200),
            )
            continue
          }
          break
        }
      } catch (e) {
        if (pollTokenRef.current === token) {
          const msg = e instanceof Error ? e.message : String(e)
          setTranslationNotice({
            tone: 'error',
            text: `翻译任务中断：${msg}`,
            action: 'retry',
          })
        }
        return
      } finally {
        if (pollTokenRef.current === token) {
          fullRunning.current = false
          setTranslating(false)

          if (terminal === 'paused') {
            setTranslationNotice({
              tone: 'warn',
              text: lastError?.message || '翻译任务已暂停，进度已保存。',
              action: 'resume',
            })
          } else if (terminal === 'failed') {
            // 引擎依赖没装好不是翻译失败：弹下载进度窗，装好后自动接着翻译。
            if (lastError?.code === 'engine_missing') {
              setTranslationNotice(null)
              setEngineDialogOpen(true)
            } else {
              setTranslationNotice({
                tone: 'error',
                text: `${lastError?.message || '翻译失败'}（${lastError?.code || 'unknown'}）`,
                action:
                  lastError?.code === 'auth' || lastError?.code === 'quota'
                    ? 'settings'
                    : 'retry',
              })
            }
          } else if (terminal === 'cancelled') {
            setTranslationNotice({
              tone: 'warn',
              text: '翻译任务已取消，已译部分保留。',
              action: 'retry',
            })
          } else if (report) {
            if (report.translated === 0) {
              setTranslationNotice({
                tone: 'error',
                text: `正文翻译完成 0/${report.total}，${report.failed} 块失败；已跳过生成空译文 PDF。`,
                action: report.failed ? 'retry' : undefined,
              })
            } else if (report.failed > 0 || report.coverage < 1) {
              const pct = Math.round(report.coverage * 100)
              setTranslationNotice({
                tone: 'warn',
                text: `已译 ${report.translated}/${report.total}（${pct}%），${report.failed} 块失败；未翻部分保留原文。`,
                action: 'retry',
              })
            } else {
              setTranslationNotice(null)
            }
          }

          if ((report?.translated ?? 0) > 0) void buildTranslatedPdf()
          void refreshTranslationManifest()
        }
      }
    },
    [
      paperId,
      applyTranslationBlocks,
      buildTranslatedPdf,
      refreshTranslationManifest,
    ],
  )

  /**
   * 全文翻译：`toggle` 是顶栏按钮语义（进入/退出双栏），`enter` 用于自动进入
   * 与「继续翻译/重试」这类不该切换视图的调用。
   */
  const translateAll = useCallback(async (action: 'toggle' | 'enter' = 'toggle') => {
    const current = paperRef.current
    if (!current || paperId === 'demo') return
    // 已在双栏：这次点击是「退出」，回到原版 PDF 阅读页（不是重排流式页）。
    // 翻译任务归服务端，退出后继续跑，重新点进来接着看进度。
    if (dualMode && action === 'toggle') {
      setTranslateStarting(false)
      setDualMode(false)
      setDualCollapsed(false)
      if (canShowPdf) {
        setPdfMode(true)
        const url = new URL(window.location.href)
        url.searchParams.set('view', 'pdf')
        window.history.replaceState({}, '', url)
      }
      return
    }
    const targetLangNow = loadTargetLang()
    const cfg = readyModel(loadModelSettings(), 'answer')
    // 点击即进入双栏：右侧先出纸张骨架，清单与任务在后台继续准备。
    if (!cfg) {
      setSettingsKind('answer')
      setSettingsOpen(true)
      return
    }
    // 点击本身负责启动翻译，标记一次让自动启动 effect 不再重复触发
    manualEnterRef.current = true
    setDualMode(true)
    setDualCollapsed(true)
    setPdfMode(false)
    setTranslateStarting(true)
    const online = await serverReachable()

    // 依赖预检：引擎还没下载好时先弹下载进度窗，不要等任务跑起来再报翻译失败。
    if (online) {
      try {
        const engine = await getEngineStatus()
        if (!engine.installed) {
          setTranslateStarting(false)
          setTranslationNotice(null)
          setEngineDialogOpen(true)
          return
        }
        if (engine.cache_ready === false) {
          // 引擎本体装好但模型/字体缓存还没下过：翻译能开始，只是首篇会慢一些。
          setTranslationNotice({
            tone: 'info',
            text: '首次整篇翻译需要下载 BabelDOC 模型与字体缓存，开始阶段可能较慢，请保持网络畅通。',
          })
        }
      } catch {
        // 状态接口不可用时不阻断，任务接口仍会返回 engine_missing
      }
    }

    // 服务端清单是唯一口径：参考文献/图内文字/公式碎片不再算「还差几块」。
    let manifest: TranslationManifest | null = null
    if (online) {
      try {
        manifest = await fetchTranslationManifest(paperId, targetLangNow)
        setTranslationManifest(manifest)
      } catch {
        // 清单拿不到时退回旧判断，不阻断翻译
      }
    }
    const legacyPending = current.blocks.some(
      (b) =>
        (b.translatable || b.kind === 'caption') &&
        b.text.trim() &&
        !(b.translated ?? '').trim(),
    )
    const pending = manifest
      ? manifest.pending > 0 || manifest.failed > 0
      : legacyPending
    // 正文已译完但缺译文 PDF 时，改发一个「只出成品」的任务
    let rebuildPdf = false

    // 双栏退出已在函数开头处理；这里只管进入：无待译块时直接展示已有译文。
    if (manifest && manifest.eligible_total === 0) {
      setTranslateStarting(false)
      const refs = manifest.excluded?.reference ?? 0
      setTranslationNotice({
        tone: 'error',
        text: refs
          ? `本篇有 ${refs} 条参考文献，但没有可翻译的正文块；参考文献按设计保留原文。`
          : '本篇没有可翻译的正文块（可能是扫描版或解析未完成），不会生成译文 PDF。',
      })
      return
    }
    if (!pending) {
      // 正文已有译文：确认译文 PDF 是否也生成了。老论文可能只有分块译文、
      // 没有成品 PDF，这时自动补一次引擎任务，让「译文/对照」直接有内容可看。
      let hasPdf = true
      if (online) {
        try {
          const head = await fetch(
            `/api/papers/${encodeURIComponent(paperId)}/translated-pdf?lang=${encodeURIComponent(
              targetLangNow,
            )}`,
            { method: 'HEAD' },
          )
          hasPdf = head.ok
        } catch {
          hasPdf = true
        }
      }
      if (hasPdf) {
        setTranslateStarting(false)
        setDualMode(true)
        setDualCollapsed(true)
        setPdfMode(false)
        return
      }
      rebuildPdf = true
    }

    // 离线：沿用本机模型通道，不进入服务端任务。
    if (!online) {
      abortRef.current?.abort()
      fullAbortRef.current?.abort()
      const ac = new AbortController()
      fullAbortRef.current = ac
      fullRunning.current = true
      setTranslateStarting(false)
      setTranslating(true)
      setDualMode(true)
      setDualCollapsed(true)
      setPdfMode(false)
      try {
        await translatePagesLocal({
          paper: current,
          pages: Array.from({ length: current.page_count }, (_, i) => i + 1),
          config: { baseUrl: cfg.baseUrl, apiKey: cfg.apiKey, model: cfg.model },
          targetLang: targetLangNow,
          signal: ac.signal,
          onPartial: (id, text) =>
            setPaper((prev) =>
              prev
                ? {
                    ...prev,
                    blocks: prev.blocks.map((b) =>
                      b.id === id ? { ...b, translated: text } : b,
                    ),
                  }
                : prev,
            ),
          onBlock: (id, text) =>
            setPaper((prev) =>
              prev
                ? {
                    ...prev,
                    translation_lang: targetLangNow,
                    blocks: prev.blocks.map((b) =>
                      b.id === id ? { ...b, translated: text } : b,
                    ),
                  }
                : prev,
            ),
        })
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        setTranslationNotice({ tone: 'error', text: `本机翻译中断：${msg}` })
      } finally {
        if (fullAbortRef.current === ac) fullAbortRef.current = null
        fullRunning.current = false
        setTranslating(false)
        void buildTranslatedPdf()
      }
      return
    }

    // 已有任务：running 直接重连；paused 点一次「翻译全文/继续」即续跑。
    let active: TranslationJob | null = null
    try {
      active = await getActiveTranslationJob(paperId, targetLangNow)
    } catch {
      // /jobs 接口不可用时再走探活 + 新建
    }
    if (active?.status === 'running') {
      setTranslationNotice(null)
      await pollTranslationJob(active, 0)
      return
    }

    // 长任务前先发一次最小请求：Key / 地址 / 网络问题不要等翻到一半才暴露。
    try {
      await probeModel(toPayload(cfg))
    } catch (e) {
      setTranslateStarting(false)
      const msg = e instanceof Error ? e.message : String(e)
      setTranslationNotice({
        tone: 'error',
        text: `模型连接失败，已取消本次翻译：${msg}`,
        action: 'settings',
      })
      setSettingsKind('answer')
      setSettingsOpen(true)
      return
    }

    try {
      const job =
        active?.status === 'paused'
          ? await resumeTranslationJob(paperId, active.job_id, toPayload(cfg))
          : await startTranslationJob(
              paperId,
              toPayload(cfg),
              [],
              targetLangNow,
              autoHighlights,
              undefined,
              undefined,
              rebuildPdf,
            )
      setTranslationNotice(null)
      await pollTranslationJob(job, 0)
    } catch (e) {
      setTranslateStarting(false)
      // 引擎未就绪不是失败：打开带进度的下载弹窗，装好后自动继续翻译。
      if (e instanceof ApiError && e.code === 'engine_missing') {
        setTranslationNotice(null)
        setEngineDialogOpen(true)
        return
      }
      const msg = e instanceof Error ? e.message : String(e)
      setTranslationNotice({
        tone: 'error',
        text: `翻译任务启动失败：${msg}`,
        action: 'retry',
      })
    }
  }, [
    dualMode,
    canShowPdf,
    paperId,
    autoHighlights,
    pollTranslationJob,
  ])

  // 打开论文 / 切回阅读页：有运行中的任务就自动接上；暂停则提示继续。
  useEffect(() => {
    if (paperId === 'demo' || !configured) return
    let alive = true
    void getActiveTranslationJob(paperId, targetLang)
      .then((job) => {
        if (!alive || !job) return
        if (job.status === 'running') {
          void pollTranslationJob(job, 0)
        } else if (job.status === 'paused') {
          setTranslationNotice({
            tone: 'warn',
            text: job.error?.message || '上次翻译已暂停，点击继续即可接着翻译。',
            action: 'resume',
          })
        }
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [paperId, targetLang, configured, pollTranslationJob])

  // 离开论文/切页时只停前端轮询，不取消服务端任务。
  useEffect(() => {
    return () => {
      pollTokenRef.current += 1
      fullRunning.current = false
    }
  }, [paperId])

  // 切换论文时清掉上一轮任务的进度，避免新论文首屏显示旧数字。
  useEffect(() => {
    setJobProgress(null)
  }, [paperId])

  // URL 直接打开 ?view=dual 时，自动开始整篇翻译（只触发一次）
  const dualAutoStarted = useRef(false)
  // 用户点顶栏进入时，点击本身已经在启动翻译，避免 effect 再启动一次
  const manualEnterRef = useRef(false)
  useEffect(() => {
    if (!dualMode || dualAutoStarted.current) return
    dualAutoStarted.current = true
    if (manualEnterRef.current) {
      manualEnterRef.current = false
      return
    }
    // 没配模型时不要自动弹设置，避免挡住双栏；用户点「翻译全文」时再引导配置
    if (readyModel(loadModelSettings(), 'answer')) void translateAll('enter')
  }, [dualMode, translateAll])

  // 进入双栏时强制刷新译文 PDF 的 URL，避免浏览器复用旧缓存
  useEffect(() => {
    if (dualMode) setTranslatedPdfVersion((v) => v + 1)
  }, [dualMode])

  // 进入双栏时记下当前页作为右栏起点；之后左右只按滚动比例联动，不再按页跳转
  useEffect(() => {
    if (dualMode) setDualStartPage(pdfPage)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dualMode])

  // 进入双栏前先确认译文 PDF 是否存在，避免 pdf.js 去加载 404
  const autoPdfBuildRef = useRef(false)
  useEffect(() => {
    if (!dualMode || !translatedPdfUrl) {
      setTranslatedPdfReady(false)
      return
    }
    let alive = true
    fetch(translatedPdfUrl, { method: 'HEAD' })
      .then((r) => {
        if (!alive) return
        setTranslatedPdfReady(r.ok)
        if (r.ok || autoPdfBuildRef.current) return
        // 已有译文但还没有译文 PDF：自动补建一次，避免每次进双栏都等
        const hasTranslation = Boolean(
          paperRef.current?.blocks.some((b) => (b.translated ?? '').trim()),
        )
        if (hasTranslation) {
          autoPdfBuildRef.current = true
          void buildTranslatedPdf()
        }
      })
      .catch(() => {
        if (alive) setTranslatedPdfReady(false)
      })
    return () => {
      alive = false
    }
  }, [dualMode, translatedPdfUrl, buildTranslatedPdf])

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
    const onResize = () => {
      setPanelWidth((width) => clampPanelWidth(width))
      const narrow = readingLayoutWidth() < AUTO_COLLAPSE_WIDTH
      if (narrow) {
        if (!narrowPanelOverride.current) setAutoCollapsed(true)
      } else {
        narrowPanelOverride.current = false
        setAutoCollapsed(false)
      }
    }
    onResize()
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

  const togglePanel = () => {
    if (readingLayoutWidth() < AUTO_COLLAPSE_WIDTH) narrowPanelOverride.current = true
    // 自动折叠时点按钮＝手动展开，不影响用户持久化的折叠偏好
    if (autoCollapsed) {
      setAutoCollapsed(false)
      return
    }
    setPanelCollapsed((prev) => {
      const next = !prev
      try {
        localStorage.setItem(PANEL_COLLAPSED_KEY, next ? '1' : '0')
      } catch {
        // 隐私模式写不了 localStorage：本次会话仍可折叠
      }
      return next
    })
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

  // ── 划词 → 问答引用 ───────────────────────────────────
  const addQuote = useCallback(
    (draft: ReaderQuoteDraft) => {
      const text = draft.text.trim()
      if (!text) return
      // 同一块（或同一页）的同一段文字只引用一次，避免重复划词塞满面板
      const id = `${draft.blockId ?? `p${draft.page ?? 0}`}:${draft.source}:${text}`
      setQuotes((prev) =>
        prev.some((one) => one.id === id)
          ? prev
          : [...prev, { ...draft, text, id }].slice(-6),
      )
      // 侧栏可能被折叠、或原版 PDF 正处于展开态（此时没有侧栏）：先恢复侧栏
      setPanelCollapsed(false)
      try {
        localStorage.setItem(PANEL_COLLAPSED_KEY, '0')
      } catch {
        // 隐私模式写不了 localStorage：本次会话仍可展开
      }
      if (pdfExpanded) {
        setPdfExpanded(false)
        const url = new URL(window.location.href)
        url.searchParams.delete('full')
        window.history.replaceState({}, '', url)
      }
      setPanelTab('chat')
      setQuoteFocus({ at: Date.now() })
    },
    [pdfExpanded],
  )
  const removeQuote = (id: string) =>
    setQuotes((prev) => prev.filter((one) => one.id !== id))
  const clearQuotes = () => setQuotes([])

  useEffect(() => {
    setContextItems([])
    setQuotes([])
    setQuoteFocus(null)
  }, [paperId])

  // ── 加载（解析没完成时自动重试） ───────────────────────
  useEffect(() => {
    let alive = true
    let timer: number | undefined
    setPaper(null)
    setError('')
    setVisiblePage(1)
    setSettledPage(1)
    setTranslationFailures({})
    setPageUrls({})
    setLocalKind('none')
    setLocalPdfUrl(null)

    setPaperLang('unknown')
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
        adoptPaperLanguage(local.paper)
        setPaper(local.paper)
        return
      }
      try {
        const p = await loadPaper(paperId)
        if (!alive) return
        setParsing(false)
        adoptPaperLanguage(p)
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
  }, [paperId, adoptPaperLanguage])

  // 旧论文解析时还没有识图模型：打开后批量补一次公式 LaTeX，不改版面、不动译文。
  // 设置里换了识图模型（settingsVersion 变化）后会重新检查没识别出来的公式。
  useEffect(() => {
    if (!paper || paperId === 'demo' || localKind !== 'none') return
    const pending = paper.blocks.some(
      (b) => b.kind === 'equation' && b.image_path && !b.latex,
    )
    if (!pending) return
    const cfg = visionModel()
    if (!cfg) return
    const key = `${paperId}:${settingsVersion}`
    if (formulaRepairTried.current.has(key)) return
    formulaRepairTried.current.add(key)

    let alive = true
    setFormulaRepairing(true)
    void recognizePaperFormulas(paperId, toPayload(cfg))
      .then((result) => {
        if (!alive || !result.items.length) return
        const latex = new Map(result.items.map((item) => [item.block_id, item.latex]))
        setPaper((prev) =>
          prev
            ? {
                ...prev,
                blocks: prev.blocks.map((b) =>
                  latex.has(b.id) ? { ...b, latex: latex.get(b.id) } : b,
                ),
              }
            : prev,
        )
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setFormulaRepairing(false)
      })
    return () => {
      alive = false
    }
  }, [paper, paperId, localKind, settingsVersion])

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

  // 设置里切换「自动翻译」总开关：原文与目标语种一致时仍保持关闭，需在顶栏手动打开
  useEffect(() => {
    const onChanged = (event: Event) => {
      const enabled = Boolean((event as CustomEvent).detail)
      applyAutoTranslate(enabled && !sameLanguage(paperLang, targetLang))
    }
    window.addEventListener('paperead:auto-translate-changed', onChanged)
    return () =>
      window.removeEventListener('paperead:auto-translate-changed', onChanged)
  }, [applyAutoTranslate, paperLang, targetLang])

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

  // 设置里切换译文语言：清掉本地旧译文，并按新目标语种重新判定自动翻译
  useEffect(() => {
    if (paperId === 'demo') return
    const onChanged = () => {
      requested.current.clear()
      abortRef.current?.abort()
      // 目标语种变了：整篇任务也不能继续用旧语种写回
      fullAbortRef.current?.abort()
      fullRunning.current = false
      pollTokenRef.current += 1
      const lang = loadTargetLang()
      setTargetLang(lang)
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
      // 目标语种变了，原文与目标是否一致也随之变化；用户手动切换过的仍以手动为准
      const manual = loadPaperAutoTranslate(paperId)
      applyAutoTranslate(
        manual ?? defaultAutoTranslateForPaper(paperLang, loadAutoTranslate(), lang),
      )
    }
    window.addEventListener('paperead:translation-changed', onChanged)
    return () =>
      window.removeEventListener('paperead:translation-changed', onChanged)
  }, [paperId, paperLang, applyAutoTranslate])

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
    // 未读论文默认从摘要开始：封面标题、作者与单位已经在 hero 里折叠，
    // 正文流不再从首页的作者/投稿信息读起。已读论文仍回到上次位置。
    if (!hash && (!progress || (!progress.block_id && !progress.pct))) {
      window.setTimeout(() => {
        const el = scrollRef.current
        const target =
          el?.querySelector<HTMLElement>('[data-block-kind="abstract"]') ??
          el?.querySelector<HTMLElement>('[data-block-kind="para"]')
        target?.scrollIntoView({ block: 'start' })
      }, 120)
      return
    }
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
    async (pages: number[], ids?: string[]) => {
      const current = paperRef.current
      if (!current || paperId === 'demo') return
      if (!autoTranslate) return
      // 整篇任务持有服务端的 paper 锁，按页再起一条只会排队等锁；
      // 这里直接让路，避免用户滚动时堆积无意义的请求。
      if (fullRunning.current) return
      const cfg = readyModel(loadModelSettings(), 'answer')
      if (!cfg) return
      const targetLang = loadTargetLang()
      // 同一时刻只跑一条流。用户快速滑动时：若正在翻的页已经离开视野，
      // 直接中止它，把配额让给用户真正停下来的那一页；否则排到队尾。
      if (inFlight.current) {
        if (!inFlightPages.current.includes(pages[0]) || (ids?.length && !inFlightBlocks.current.includes(ids[0]))) {
          abortRef.current?.abort()
        }
        queued.current = { pages, ids }
        return
      }

      const sameLang = (current.translation_lang || 'zh-Hans') === targetLang
      const need = pages.filter((pg) => {
        if (requested.current.has(pg)) return false
        const blocks = current.blocks.filter(
          (b) =>
            b.page === pg
            && (!ids || ids.includes(b.id))
            // 图注/表注随论文一起翻，历史论文的 caption 块也要放行；
            // 服务端判定为 skipped（参考文献/图内文字/公式碎片）的块不再反复请求。
            && (b.translatable || b.kind === 'caption')
            && manifestStatus.get(b.id) !== 'skipped'
            && b.text.trim(),
        )
        if (!blocks.length) return false
        // 目标语言变了：即使本地已有旧译文，也要重新请求
        return !sameLang || blocks.some((b) => !b.translated)
      })
      if (!need.length) return

      // 服务器不可达时走本机：模型请求由 Rust 发出，逐段流式回填
      inFlight.current = true
      inFlightPages.current = pages
      inFlightBlocks.current = ids ?? []
      const online = await serverReachable()
      // await 期间（最长 2.5s）用户可能已经关掉自动翻译：此时不能再
      // 新建一条翻译流，否则顶栏会重新出现「翻译中」且请求照发。
      if (!autoTranslateRef.current || (!online && !canRunLocally())) {
        if (autoTranslateRef.current && ids?.length) {
          setTranslationFailures((prev) => {
            const next = { ...prev }
            for (const id of ids) next[id] = (next[id] ?? 0) + 1
            return next
          })
        }
        inFlight.current = false
        inFlightPages.current = []
        inFlightBlocks.current = []
        return
      }
      if (queued.current) {
        const next = queued.current
        queued.current = null
        inFlight.current = false
        inFlightPages.current = []
        inFlightBlocks.current = []
        window.setTimeout(() => void ensurePages(next.pages, next.ids), 0)
        return
      }

      need.forEach((pg) => requested.current.add(pg))
      inFlight.current = true
      inFlightPages.current = need
      const ac = new AbortController()
      abortRef.current = ac
      setTranslating(true)
      const completed = new Set<string>()
      let interrupted = false
      let timedOut = false
      const timeout = window.setTimeout(() => { timedOut = true; ac.abort() }, 60000)
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
            blockIds: ids,
            config: { baseUrl: cfg.baseUrl, apiKey: cfg.apiKey, model: cfg.model },
            targetLang,
            signal: ac.signal,
            onPartial: apply,
            onBlock: (blockId, text) => { completed.add(blockId); apply(blockId, text) },
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
              completed.add(updated.id)
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
          ids,
        )
      } catch (e) {
        interrupted = ac.signal.aborted && !timedOut
        if (!(e instanceof DOMException && e.name === 'AbortError')) {
          console.warn('翻译流中断：', e)
        }
        // 失败/中断都撤销标记，交给看门狗重试
        need.forEach((pg) => requested.current.delete(pg))
      } finally {
        window.clearTimeout(timeout)
        if (!interrupted && ids?.length) {
          setTranslationFailures((prev) => {
            const next = { ...prev }
            for (const id of ids) {
              if (completed.has(id)) delete next[id]
              else next[id] = (next[id] ?? 0) + 1
            }
            return next
          })
        }
        need.forEach((pg) => requested.current.delete(pg))
        inFlight.current = false
        inFlightPages.current = []
        inFlightBlocks.current = []
        abortRef.current = null
        setTranslating(false)
        const next = queued.current as { pages: number[]; ids?: string[] } | null
        queued.current = null
        if (next) window.setTimeout(() => void ensurePages(next.pages, next.ids), 0)
      }
    },
    [paperId, mergeHighlights, autoHighlights, autoTranslate, manifestStatus],
  )

  // 进入阅读页先翻当前页 + 下一页；滚动停稳后换页继续增量翻
  useEffect(() => {
    if (!paper) return
    const page = pdfMode ? pdfPage : settledPage
    const eligible = paper.blocks.filter(
      (b) =>
        (b.translatable || b.kind === 'caption') &&
        manifestStatus.get(b.id) !== 'skipped' &&
        b.text.trim(),
    )
    const ids = priorityBlocks(paper.blocks, visibleBlocks, pdfMode, page)
    const selected = eligible.filter((b) => ids.includes(b.id) && !b.translated)
    if (selected.length) void ensurePages([...new Set(selected.map((b) => b.page))], selected.map((b) => b.id))
  }, [paper, settledPage, pdfMode, pdfPage, visibleBlocks, ensurePages, translationRetry, manifestStatus])

  // 滚动停稳 700ms 才算「用户停留」，快速滑动过程中不触发翻译
  useEffect(() => {
    const timer = window.setTimeout(() => setSettledPage(visiblePage), 700)
    return () => window.clearTimeout(timer)
  }, [visiblePage])

  // 看门狗：每 3 秒检查当前页是否还有没翻的块。
  // 流被中断、429、网络抖动都能自愈，不会出现"一直转圈但不动"。
  useEffect(() => {
    if (paperId === 'demo' || !autoTranslate) return
    const timer = window.setInterval(() => {
      if (inFlight.current) return
      setTranslationRetry((value) => value + 1)
    }, 3000)
    return () => window.clearInterval(timer)
  }, [paperId, autoTranslate])

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
      const bounds = el.getBoundingClientRect()
      const ids = Array.from(el.querySelectorAll<HTMLElement>('[data-block-id], [data-heading-id]'))
        .filter((node) => {
          const rect = node.getBoundingClientRect()
          return rect.bottom > bounds.top && rect.top < bounds.bottom
        }).map((node) => node.id)
      setVisibleBlocks((prev) => prev.length === ids.length && prev.every((id, i) => id === ids[i]) ? prev : ids)
    }

    el.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    onScroll()
    return () => { el.removeEventListener('scroll', onScroll); window.removeEventListener('resize', onScroll) }
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

  /** 双击正文里的荧光笔/批注：左侧切到「批注」，并高亮对应卡片。 */
  const openAnnotation = useCallback((annotation: Annotation) => {
    setPanelCollapsed(false)
    setPanelTab('comments')
    setFocusAnnotation({ id: annotation.id, at: Date.now() })
  }, [])

  /** 双击自动阅读重点：左侧切到「阅读重点」，并定位对应条目。 */
  const openHighlight = useCallback((highlight: ReadingHighlight) => {
    setPanelCollapsed(false)
    setPanelTab('key')
    setFocusHighlight({ id: highlight.id, at: Date.now() })
  }, [])

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

  /** PDF 视图 / 双栏的「原文 · 译文 · 对照」切换（顶栏组件）。 */
  const setPdfReadingMode = (next: 'source' | 'zh' | 'dual') => {
    if (next === 'source') {
      // 已经在原文页就保持；在双栏则退出回原版 PDF
      if (dualMode) void translateAll('toggle')
      return
    }
    setDualShowSource(next === 'dual')
    if (!dualMode) void translateAll('enter')
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

  const togglePdfReview = () => {
    const next = !pdfReview
    setPdfReview(next)
    try {
      localStorage.setItem(PDF_REVIEW_KEY, next ? '1' : '0')
    } catch {
      // 写不了就只在本次会话生效
    }
  }

  /** Ctrl/⌘ + 滚轮：原版 PDF 改 pdfZoom，对比阅读两栏共用 dualZoom。 */
  const zoomByWheel = (delta: number) => {
    if (dualMode) setDualZoom((z) => clampZoom(z + delta * 0.1))
    else setPdfZoom((z) => clampZoom(z + delta * 0.1))
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
        <button
          className="icon-btn splash-back"
          onClick={onBack}
          title="返回文献库"
          aria-label="返回文献库"
        >
          <IconBack />
        </button>
        <div>
          <div className="spinner" />
          {parsing ? '正在解析版面、图表和公式…' : '正在加载论文…'}
        </div>
      </div>
    )
  }

  const legacyTranslated = paper.blocks.filter((b) => (b.translated ?? '').trim()).length
  const legacyTotal = paper.blocks.filter(
    (b) => (b.translatable || b.kind === 'caption') && b.text.trim(),
  ).length
  // 分母用服务端可译清单：参考文献/图内文字不再被算成「没翻」。
  const manifestTotal = translationManifest?.eligible_total ?? legacyTotal
  const manifestDone = translationManifest?.translated ?? legacyTranslated
  // 运行中的任务进度优先于清单：分块任务（离线/按页）的进度会由服务端逐段递增，
  // 而清单要等任务落盘后才刷新，拿清单顶替会停在 0。
  const jobRunning = translating || translateStarting || pdfBuilding
  const jobScope =
    jobRunning && jobProgress && (jobProgress.total > 0 || jobProgress.stage)
      ? jobProgress
      : null
  // 离线/分块翻译的兜底计数：只有拿不到引擎阶段时才展示块数。
  const progressTotal = Math.max(0, jobScope ? jobScope.total : manifestTotal)
  const progressDone = Math.min(
    jobScope ? jobScope.done : manifestDone,
    progressTotal,
  )
  // PDF 全文翻译由外部引擎完成，它按自己的阶段与段落推进。
  const engineStage = jobScope?.stage
  const engineStageDone = jobScope?.stageDone ?? 0
  const engineStageTotal = jobScope?.stageTotal ?? 0
  // 顶栏「已翻译 x/y 段」只用于流式阅读，统一取可译清单口径；
  // PDF 阅读/对照模式不显示块数（引擎的块数是映射估算，不能当真实计数）。
  const translated = manifestDone
  const total = manifestTotal
  const showTranslationCount = !pdfMode && !dualMode
  // 右栏骨架阶段：翻译中 → 译文 PDF 生成中 → 还没开始
  const skeletonPhase: 'translating' | 'building' | 'idle' =
    translating || translateStarting
      ? 'translating'
      : pdfBuilding
        ? 'building'
        : 'idle'

  const outline = outlineOf(paper.blocks)
  const points = keyPoints(paper.blocks)
  const sampleImage =
    paper.blocks.find((b) => b.kind === 'equation' && b.image_path)?.image_path ?? undefined
  const formulaPending = paper.blocks.some(
    (b) => b.kind === 'equation' && b.image_path && !b.latex,
  )
  const hasVision = Boolean(visionModel())

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
            // 原版视图下 ± 与 Ctrl+滚轮一致，都改页面放大倍率
            setPdfZoom((z) => clampZoom(z + (v > scale ? 0.1 : -0.1)))
          } else {
            setScale(Math.min(1.35, Math.max(0.85, v)))
          }
        }}
        translated={translated}
        total={total}
        showTranslationCount={showTranslationCount}
        onOpenSettings={() => {
          setSettingsKind('answer')
          setSettingsOpen(true)
        }}
        configured={configured}
        onBack={onBack}
        hasTranslation={translated > 0}
        translating={translating}
        page={pdfMode || dualMode ? pdfPage : visiblePage}
        canShowPdf={canShowPdf}
        pdfMode={pdfMode}
        onTogglePdf={togglePdf}
        pdfHref={pdfHref}
        pdfExpanded={pdfExpanded}
        onTogglePdfExpand={togglePdfExpand}
        pdfReview={pdfReview}
        narrow={narrowLayout}
        onTogglePdfReview={togglePdfReview}
        panelCollapsed={panelHidden}
        onTogglePanel={togglePanel}
        showPanelToggle={!(pdfMode && pdfExpanded)}
        autoHighlights={autoHighlights}
        onToggleAutoHighlights={() => saveAutoHighlights(!autoHighlights)}
        autoTranslate={autoTranslate}
        onToggleAutoTranslate={toggleAutoTranslate}
        dualMode={dualMode}
        onDualViewMode={setPdfReadingMode}
        dualShowSource={dualShowSource}
        dualSync={dualSync}
        onToggleDualSync={() => setDualSync((v) => !v)}
        dualZoom={dualZoom}
        onDualZoom={(delta) => setDualZoom((z) => clampZoom(z + delta))}
        onDualZoomReset={() => setDualZoom(1)}
        pdfPan={pdfPan}
        onTogglePdfPan={() => setPdfPan((v) => !v)}
        pdfZoom={pdfZoom}
        onPdfZoomReset={() => setPdfZoom(1)}
        translatedPdfReady={translatedPdfReady}
        translatedPdfUrl={translatedPdfUrl}
        translationLang={targetLang}
      />

      <EngineDownloadDialog
        open={engineDialogOpen}
        onClose={() => setEngineDialogOpen(false)}
        onReady={() => {
          setEngineDialogOpen(false)
          void translateAll('enter')
        }}
      />

      {translationNotice && (
        <div
          className={`formula-notice translation-notice is-${translationNotice.tone}`}
          role="status"
        >
          <span>{translationNotice.text}</span>
          {translationNotice.action === 'retry' && (
            <button
              type="button"
              onClick={() => {
                setTranslationNotice(null)
                void translateAll('enter')
              }}
            >
              重试失败块
            </button>
          )}
          {translationNotice.action === 'resume' && (
            <button
              type="button"
              onClick={() => {
                setTranslationNotice(null)
                void translateAll('enter')
              }}
            >
              继续翻译
            </button>
          )}
          {translationNotice.action === 'settings' && (
            <button
              type="button"
              onClick={() => {
                setSettingsKind('answer')
                setSettingsOpen(true)
              }}
            >
              去设置
            </button>
          )}
          <button type="button" onClick={() => setTranslationNotice(null)}>
            关闭
          </button>
        </div>
      )}

      {formulaPending && !pdfMode && !dualMode && (
        <div className="formula-notice">
          <span>
            {hasVision
              ? formulaRepairing
                ? '正在把公式截图转换为 LaTeX…'
                : '部分公式暂时无法识别，保留为截图'
              : '公式当前以截图显示'}
          </span>
          {!hasVision && (
            <button
              type="button"
              onClick={() => {
                setSettingsKind('vision')
                setSettingsOpen(true)
              }}
            >
              配置识图模型后自动转成 LaTeX →
            </button>
          )}
        </div>
      )}

      <div
        ref={workspaceRef}
        className={`workspace ${pdfMode && pdfExpanded ? 'is-pdf-expanded' : ''}${
          panelHidden ? ' is-panel-collapsed' : ''
        }`}
        style={{ '--panel-w': `${panelWidth}px` } as CSSProperties}
      >
        {!(pdfMode && pdfExpanded) && !panelHidden && (
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
focusAnnotation={focusAnnotation}
              focusHighlight={focusHighlight}
              contextItems={contextItems}
              onRemoveContext={removeContext}
              onClearContext={clearContext}
              quotes={quotes}
              onRemoveQuote={removeQuote}
              onClearQuotes={clearQuotes}
              quoteFocus={quoteFocus}
              onCollapse={togglePanel}
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
        {dualMode ? (
          <DualReader
            showSource={dualShowSource}
            syncEnabled={dualSync}
            translationReady={translatedPdfReady}
            zoom={dualZoom}
            progress={
              translatedPdfReady || skeletonPhase === 'idle'
                ? null
                : {
                    done: progressDone,
                    total: progressTotal,
                    building: skeletonPhase === 'building',
                    stage: engineStage,
                    stageDone: engineStageDone,
                    stageTotal: engineStageTotal,
                  }
            }
            pdfPane={
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
                  zoom={dualZoom}
                  pan={pdfPan}
                  onZoomWheel={zoomByWheel}
                  jump={pdfJump}
                  onPageChange={setPdfPage}
                  pageUrls={pageUrls}
                  pdfUrl={pdfHref}
                  annotations={annotations}
                  annotationActions={annotationActions}
                  onOpenAnnotation={openAnnotation}
                  onAskQuote={addQuote}
                  reviewMode={false}
                  focusAnnotation={focusAnnotation}
                />
              </Suspense>
            }
            translationPane={
              translatedPdfReady ? (
                <Suspense
                  fallback={
                    <div className="pdf-view">
                      <div className="pdf-pages">
                        <p className="pdf-page-hint">正在准备译文 PDF…</p>
                      </div>
                    </div>
                  }
                >
                  <PdfPages
                    paperId={paperId}
                    pageCount={paper.page_count}
                    pageSizes={paper.page_sizes}
                    initialPage={dualStartPage}
                    width={pdfWidth}
                    zoom={dualZoom}
                    pan={pdfPan}
                    onZoomWheel={zoomByWheel}
                    onPageChange={() => undefined}
                    pageUrls={{}}
                    pdfUrl={translatedPdfUrl}
                    annotations={[]}
                    reviewMode={false}
                  />
                </Suspense>
              ) : (
                <TranslationSkeleton
                  pageCount={paper.page_count}
                  pageSizes={paper.page_sizes}
                  currentPage={pdfPage}
                  phase={skeletonPhase}
                  done={progressDone}
                  total={progressTotal}
                  stageText={
                    engineStage
                      ? engineStageText(
                          engineStage,
                          engineStageDone,
                          engineStageTotal,
                          '正在翻译全文',
                        )
                      : ''
                  }
                />
              )
            }
          />
        ) : pdfMode ? (
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
              zoom={pdfZoom}
              pan={pdfPan}
              onZoomWheel={zoomByWheel}
              jump={pdfJump}
              onPageChange={setPdfPage}
              pageUrls={pageUrls}
              pdfUrl={pdfHref}
              annotations={annotations}
              annotationActions={annotationActions}
              onOpenAnnotation={openAnnotation}
              onAskQuote={addQuote}
              reviewMode={pdfReview && !narrowLayout}
              focusAnnotation={focusAnnotation}
            />
          </Suspense>
        ) : (
          <Reader
            paper={paper}
            mode={mode}
            scale={scale}
            scrollRef={scrollRef}
            canTranslate={configured}
            translationFailures={translationFailures}
            onRetryTranslation={(id) => {
              setTranslationFailures((prev) => { const next = { ...prev }; delete next[id]; return next })
              setTranslationRetry((value) => value + 1)
            }}
            autoTranslate={autoTranslate}
            onOpenSettings={() => {
              setSettingsKind('answer')
              setSettingsOpen(true)
            }}
hint={
              !configured
                ? '未配置回答模型，翻译未开启。填写模型与密钥后，打开论文会自动翻译当前页和下一页。'
                : sameLanguage(paperLang, targetLang) && !autoTranslate
                  ? `本篇原文是${PAPER_LANG_LABELS[paperLang]}，与目标译文语言一致，已默认关闭翻译。可在顶栏开启自动翻译。`
                  : !autoTranslate
                    ? '自动翻译已关闭，可在顶栏开启自动翻译。'
                    : translating
                      ? '正在翻译当前页…'
                      : '当前页还没有译文，停在这里几秒会自动开始翻译。'
            }
            annotations={annotations}
            annotationActions={annotationActions}
            highlights={highlights}
            contextIds={contextIds}
            onAttachContext={attachContext}
            onAskQuote={addQuote}
            onOpenAnnotation={openAnnotation}
            onOpenHighlight={openHighlight}
          />
        )}
      </div>

      {(!(pdfMode && pdfExpanded) || narrowLayout) && (
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
          focusAnnotation={focusAnnotation}
          focusHighlight={focusHighlight}
          contextItems={contextItems}
          onRemoveContext={removeContext}
          onClearContext={clearContext}
          quotes={quotes}
          onRemoveQuote={removeQuote}
          onClearQuotes={clearQuotes}
          quoteFocus={quoteFocus}
          focusChat={quoteFocus}
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
