import { useEffect, useMemo, useRef, useState } from 'react'
import type { Paper, ReaderContextItem } from '../types'
import {
  askPaperStream,
  getChat,
  listPaperAttachments,
  uploadPaperAttachment,
  type AskSource,
  type AskToolCall,
  type AskWebStatus,
  type PaperAttachment,
} from '../lib/library'
import {
  isModelReady,
  loadModelSettings,
  readyModel,
  saveModelSettings,
  toPayload,
  type ModelSettings,
} from '../lib/settings'
import { buildAnswer, type AskKind } from '../lib/assistant'
import { canRunLocally, serverReachable } from '../lib/connectivity'
import { streamLocalChat } from '../lib/localLlm'
import { buildLocalContext, buildLocalIndex, retrieveLocal } from '../lib/localSearch'
import { appendLocalChat, loadLocalChat } from '../lib/offlineStore'
import { tourBus } from '../tour/tourBus'
import { Markdown, balanceMarkdown } from './Markdown'
import { Katex } from './Katex'
import { ReaderToolCard } from './ReaderToolCard'
import {
  IconCheck,
  IconClose,
  IconFolder,
  IconPlus,
  IconSend,
  IconSettings,
  IconSpark,
  IconStop,
} from './icons'

type TurnStatus = 'retrieving' | 'streaming' | 'done' | 'error' | 'stopped'

function sizeText(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  if (size >= 1024) return `${Math.round(size / 1024)} KB`
  return `${size} B`
}

interface Turn {
  id: number
  question: string
  answer: string
  sources: AskSource[]
  usedWeb: boolean
  /** 后端联网过程的结构化结果（搜到几条、用了哪个引擎）；离线链路没有这一项 */
  webStatus?: AskWebStatus
  /** 检索步骤：paper_search / web_search，渲染成研究页同款工具卡片 */
  tools: AskToolCall[]
  latency?: number
  status: TurnStatus
  error?: string
  /** 本轮随问题上传的附件（仅当前会话内展示，刷新后不恢复） */
  attachments?: PaperAttachment[]
  /** 本地演示答案：不是模型生成，按纯文本渲染，保留换行 */
  demo?: boolean
  /** 本轮随问题引用的图表/公式（阅读页小圆圈加入的附录） */
  contexts?: ReaderContextItem[]
  /** 后端历史里的轮次 id（恢复来的轮次才有；往前翻页时作为 before） */
  memoryId?: string
}

/** 本机问答的系统提示：和电脑端 `paperead/tutor/qa.py` 保持一致。 */
const LOCAL_ANSWER_SYS = `你是论文助教。结合【材料】回答问题，用中文，简洁、分点、不要客套。

规则：
1. 优先用论文材料回答；论文没写、或用户问的是论文之外的知识时，可以补充必要的领域常识与定义，不要因为论文没写就拒答；
2. 绝对不要编造论文里没有的页码、数据、实验结论；
3. 来自论文材料的论断标 [P3]；来自你自己领域常识的，句末标「（通识）」；
4. 论文材料与常识冲突时，指出各自说法，不要强行统一；
5. 材料完全不足以回答时，说明缺什么，再给出基于常识的最佳解释，不要只回一句"论文中没有提到"；
6. 不要复述问题，直接给答案。`

const ENGINE_LABEL: Record<string, string> = {
  tavily: 'Tavily',
  so360: '360 搜索',
  bing: 'Bing',
  duckduckgo: 'DuckDuckGo',
  openalex: 'OpenAlex',
}

/** 联网结果的展示文案：让用户知道「搜过没搜过、搜到几条」，而不是只看到是否联网。 */
function webMetaText(webStatus?: AskWebStatus, usedWeb?: boolean): string {
  if (webStatus?.attempted) {
    if (webStatus.count > 0) {
      const names = (webStatus.engines ?? [])
        .map((e) => ENGINE_LABEL[e] ?? e)
        .filter(Boolean)
      const suffix = names.length ? `（${names.join(' / ')}）` : ''
      return ` · 联网检索 ${webStatus.count} 条${suffix}`
    }
    return ' · 联网无结果，已用领域常识补充'
  }
  return usedWeb ? ' · 已联网检索' : ''
}

/** 从出处重建工具卡片：旧历史或离线记录没有 tools 字段时也要有同样的渲染。 */
function toolsFromSources(sources: AskSource[], usedWeb: boolean): AskToolCall[] {
  const paper = sources.filter((s) => s.type === 'paper')
  const web = sources.filter((s) => s.type === 'web')
  const out: AskToolCall[] = []
  if (paper.length > 0) {
    out.push({
      callId: 'paper_search',
      name: 'paper_search',
      status: 'done',
      summary: `命中 ${paper.length} 段`,
      items: paper.map((s) => ({
        label: s.label,
        page: s.page,
        blockId: s.blockId,
        snippet: s.snippet,
      })),
      meta: { hits: paper.length },
    })
  }
  if (usedWeb || web.length > 0) {
    out.push({
      callId: 'web_search',
      name: 'web_search',
      status: 'done',
      summary: web.length ? `联网检索 ${web.length} 条` : '未获取到网络结果',
      items: web.map((s) => ({
        label: s.label,
        title: s.title,
        url: s.url,
        snippet: s.snippet,
        engine: s.engine,
      })),
      meta: {
        count: web.length,
        engines: [
          ...new Set(
            web
              .map((s) => s.engine)
              .filter((x): x is string => Boolean(x)),
          ),
        ],
      },
    })
  }
  return out
}

const QUICK: { key: AskKind; label: string }[] = [
  { key: 'summary', label: '总结论文' },
  { key: 'outline', label: '梳理论文大纲' },
  { key: 'guide', label: '生成一份学习指南' },
  { key: 'faq', label: '常见问题解答' },
]

export function ChatPanel({
  paper,
  paperId,
  onJump,
  onOpenSettings,
  settingsVersion = 0,
  contextItems = [],
  onRemoveContext,
  onClearContext,
}: {
  paper: Paper
  paperId: string
  onJump?: (id: string) => void
  onOpenSettings?: () => void
  settingsVersion?: number
  contextItems?: ReaderContextItem[]
  onRemoveContext?: (id: string) => void
  onClearContext?: () => void
}) {
  const [turns, setTurns] = useState<Turn[]>([])
  // 演示论文没有后端历史，直接算就绪
  const [historyReady, setHistoryReady] = useState(paperId === 'demo')
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [useWeb, setUseWeb] = useState(true)
  const [attachments, setAttachments] = useState<PaperAttachment[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [dragging, setDragging] = useState(false)
  const [settings, setSettings] = useState<ModelSettings>(loadModelSettings)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [libraryOpen, setLibraryOpen] = useState(false)
  const [libraryItems, setLibraryItems] = useState<PaperAttachment[]>([])
  const [libraryLoading, setLibraryLoading] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)
  const libraryRef = useRef<HTMLDivElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const canUpload = paperId !== 'demo'
  const seq = useRef(0)
  const busyRef = useRef(false)
  const stickRef = useRef(true)
  const abortRef = useRef<AbortController | null>(null)

  const modelOptions = useMemo(
    () => settings.models.filter((m) => m.kind === 'answer' && isModelReady(m)),
    [settings],
  )
  const current = useMemo(() => readyModel(settings, 'answer'), [settings])

  const patch = (id: number, fn: (t: Turn) => Turn) =>
    setTurns((prev) => prev.map((t) => (t.id === id ? fn(t) : t)))

  // 只在用户没有主动上翻时贴底，避免流式回答把人从正在读的位置拽走
  useEffect(() => {
    const el = listRef.current
    if (!el || !stickRef.current) return
    el.scrollTop = el.scrollHeight
  }, [turns, busy])

  // 设置弹窗保存后刷新可选模型
  useEffect(() => {
    if (!settingsVersion) return
    setSettings(loadModelSettings())
  }, [settingsVersion])

  useEffect(() => {
    if (!pickerOpen) return
    const onDown = (e: MouseEvent) => {
      if (!pickerRef.current?.contains(e.target as Node)) setPickerOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [pickerOpen])

  // 附件库勾选器：每次打开都重新拉取，避免显示已被别处删除的附件
  useEffect(() => {
    if (!libraryOpen) return
    const onDown = (e: MouseEvent) => {
      if (!libraryRef.current?.contains(e.target as Node)) setLibraryOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [libraryOpen])

  useEffect(() => {
    if (!libraryOpen || !canUpload) return
    let alive = true
    setLibraryLoading(true)
    listPaperAttachments(paperId)
      .then((rows) => {
        if (alive) setLibraryItems(rows)
      })
      .catch(() => {
        if (alive) setLibraryItems([])
      })
      .finally(() => {
        if (alive) setLibraryLoading(false)
      })
    return () => {
      alive = false
    }
  }, [libraryOpen, paperId, canUpload])

  useEffect(() => () => abortRef.current?.abort(), [])

  // 挂载（或换论文）时恢复上次的对话；演示论文没有历史，直接标记就绪
  useEffect(() => {
    if (paperId === 'demo') {
      setHistoryReady(true)
      return
    }
    let alive = true
    setHistoryReady(false)

    const restore = async () => {
      const restoreRows = (rows: {
        id: string
        question: string
        answer: string
        sources?: AskSource[]
        usedWeb?: boolean
        tools?: AskToolCall[]
        latency?: number
      }[]) => {
        if (!alive || rows.length === 0) return
        const restored: Turn[] = rows.map((t): Turn => {
          seq.current += 1
          const sources = t.sources ?? []
          return {
            id: seq.current,
            memoryId: t.id,
            question: t.question,
            answer: t.answer ?? '',
            sources,
            usedWeb: !!t.usedWeb,
            tools:
              t.tools && t.tools.length > 0
                ? t.tools
                : toolsFromSources(sources, !!t.usedWeb),
            latency: t.latency,
            // 上一轮没答完（例如中途停止）时 answer 是空串，按 stopped 展示
            status: t.answer ? 'done' : 'stopped',
          }
        })
        // 恢复只填补空白面板：已经有（或刚发出的）对话就不要再插历史
        setTurns((prev) => (prev.length === 0 ? restored : prev))
      }

      // 离开电脑后没有服务端历史，改读本机保存的对话
      if (!(await serverReachable())) {
        const rows = await loadLocalChat(paperId).catch(() => [])
        restoreRows(
          rows.map((row) => ({
            id: row.id,
            question: row.question,
            answer: row.answer,
            sources: row.sources as AskSource[],
            usedWeb: row.usedWeb,
            latency: row.latency,
          })),
        )
        return
      }

      try {
        const { turns: history } = await getChat(paperId)
        // 旧后端/关闭记忆时可能只有 {messages} 没有 turns，按空历史处理
        restoreRows(history ?? [])
      } catch (e) {
        // 历史读不出来不影响问答
        console.warn('恢复历史对话失败', e)
      }
    }

    void restore().finally(() => {
      if (alive) setHistoryReady(true)
    })
    return () => {
      alive = false
    }
  }, [paperId])

  const onScroll = () => {
    const el = listRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }

  const chooseModel = (id: string) => {
    const next = { ...settings, active: { ...settings.active, answer: id } }
    saveModelSettings(next)
    setSettings(next)
    setPickerOpen(false)
  }

  const addFiles = async (files: File[]) => {
    if (!canUpload || files.length === 0) return
    setUploading(true)
    setUploadError('')
    for (const file of files.slice(0, 6)) {
      try {
        const item = await uploadPaperAttachment(paperId, file)
        setAttachments((prev) => [...prev, item])
        // 上传即入论文附件库，同步到勾选器里，避免打开时看不到刚传的文件
        setLibraryItems((prev) =>
          prev.some((one) => one.name === item.name) ? prev : [item, ...prev],
        )
      } catch (error) {
        setUploadError(error instanceof Error ? error.message : '附件上传失败')
      }
    }
    setUploading(false)
  }

  const toggleLibraryFile = (item: PaperAttachment) => {
    setAttachments((prev) =>
      prev.some((one) => one.name === item.name)
        ? prev.filter((one) => one.name !== item.name)
        : [...prev, item],
    )
  }

  const ask = async (
    question: string,
    kind?: AskKind,
    files: PaperAttachment[] = attachments,
    contexts: ReaderContextItem[] = contextItems,
  ) => {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    stickRef.current = true

    seq.current += 1
    const id = seq.current
    const cfg = readyModel(loadModelSettings(), 'answer')
    const sentFiles = files.slice()
    const sentContexts = contexts.slice()

    setTurns((prev) => [
      ...prev,
      {
        id,
        question,
        answer: '',
        sources: [],
        usedWeb: false,
        tools: [],
        status: 'retrieving',
        demo: !cfg,
        attachments: sentFiles.length > 0 ? sentFiles : undefined,
        contexts: sentContexts.length > 0 ? sentContexts : undefined,
      },
    ])
    setAttachments([])
    onClearContext?.()

    // 没配置回答模型时退回本地演示答案
    if (!cfg) {
      const text = kind
        ? buildAnswer(kind, paper)
        : '还没有配置回答模型，暂时无法检索论文回答。\n点下面的「配置回答模型」填入模型与密钥即可开始问答。'
      window.setTimeout(() => {
        patch(id, (t) => ({ ...t, answer: text, status: 'done' }))
        busyRef.current = false
        setBusy(false)
      }, 250)
      return
    }

    const controller = new AbortController()
    abortRef.current = controller

    // 每次提问生成新的 client id 交给服务端做幂等标记：同一 id 的手动重试会回放已存
    // 答案，避免重复落盘；这里不复用旧 id（每次发送都是新问题）。
    const clientId =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`

    // 增量先攒在 buffer 里，每 50ms 合并一次 setState：
    // 既能逐字出现，又不会因为每个 token 重渲染整段 Markdown 而抖动。
    let buffer = ''
    let timer: number | null = null
    let streamError = ''
    let gotDelta = false

    const flush = () => {
      if (timer !== null) {
        window.clearTimeout(timer)
        timer = null
      }
      if (!buffer) return
      const chunk = buffer
      buffer = ''
      patch(id, (t) => ({ ...t, answer: t.answer + chunk }))
    }

    const onDelta = (chunk: string) => {
      buffer += chunk
      if (!gotDelta) {
        gotDelta = true
        patch(id, (t) => ({ ...t, status: 'streaming' }))
      }
      if (timer === null) {
        timer = window.setTimeout(() => {
          timer = null
          flush()
        }, 50)
      }
    }

    try {
      // 服务器不可达时改走本机检索 + 本机直连模型，仍然是流式输出
      if (!(await serverReachable())) {
        if (!canRunLocally()) {
          throw new Error('电脑端服务不可达，本机也没有可用的模型通道')
        }
        const index = buildLocalIndex(paper)
        const hits = retrieveLocal(index, question)
        // 总结/大纲这类问题可能一个关键词都命中不了：退化为取前几段
        const chosen = hits.length
          ? hits
          : index.docs.slice(0, 6).map((doc) => ({ doc, score: 0 }))
        const context = buildLocalContext(index, chosen)
        const sources: AskSource[] = hits.map((hit) => ({
          type: 'paper',
          label: `[P${hit.doc.page}]`,
          page: hit.doc.page,
          blockId: hit.doc.blockId,
          snippet: (hit.doc.translated || hit.doc.text).slice(0, 160),
        }))
        patch(id, (t) => ({
          ...t,
          sources,
          tools: toolsFromSources(sources, false),
          status: 'streaming',
        }))

        const history = await loadLocalChat(paperId).catch(() => [])
        const recent = history
          .slice(-6)
          .map((row) => `用户：${row.question}\n助手：${row.answer.slice(0, 300)}`)
          .join('\n')
        const started = performance.now()
        const answer = await streamLocalChat({
          config: { baseUrl: cfg.baseUrl, apiKey: cfg.apiKey, model: cfg.model },
          messages: [
            { role: 'system', content: LOCAL_ANSWER_SYS },
            {
              role: 'user',
              content: `${recent ? `【最近对话】\n${recent}\n\n` : ''}【材料】\n${
                context || '（没有检索到相关段落）'
              }\n\n【问题】${question}`,
            },
          ],
          temperature: 0.2,
          maxTokens: 4000,
          signal: controller.signal,
          onDelta,
        })
        flush()
        const latency = (performance.now() - started) / 1000
        patch(id, (t) => ({ ...t, status: 'done', latency }))
        void appendLocalChat(paperId, {
          id: clientId,
          question,
          answer,
          sources,
          usedWeb: false,
          latency,
          at: Date.now(),
        })
        tourBus.emit('reader:chat-answered', { question, paperId })
        return
      }

      await askPaperStream(
        paperId,
        toPayload(cfg),
        question,
        useWeb,
        (event) => {
          switch (event.type) {
            case 'tool': {
              // 检索步骤按 callId 增量更新：start 先占位「运行中」，end 补结果
              const call: AskToolCall = {
                callId: event.callId ?? `tool-${Date.now()}`,
                name: event.name ?? 'tool',
                status:
                  event.phase === 'end' ? (event.status ?? 'done') : 'running',
                input: event.input,
                summary: event.summary,
                items: event.items,
                error: event.error,
                meta: event.meta,
                startedAt: event.startedAt,
                endedAt: event.endedAt,
              }
              patch(id, (t) => {
                const index = t.tools.findIndex((x) => x.callId === call.callId)
                const tools =
                  index >= 0
                    ? t.tools.map((x, i) => (i === index ? { ...x, ...call } : x))
                    : [...t.tools, call]
                return {
                  ...t,
                  tools,
                  status: t.answer ? t.status : 'streaming',
                }
              })
              break
            }
            case 'sources':
              patch(id, (t) => ({
                ...t,
                sources: event.sources ?? [],
                usedWeb: Boolean(event.usedWeb),
                webStatus: event.webStatus,
                tools:
                  event.tools && event.tools.length > 0 && t.tools.length === 0
                    ? event.tools
                    : t.tools,
                status: t.answer ? t.status : 'streaming',
              }))
              break
            case 'delta':
              if (event.text) onDelta(event.text)
              break
            case 'done':
              flush()
              patch(id, (t) => ({ ...t, status: 'done', latency: event.latency }))
              break
            case 'error':
              streamError = event.message || '回答失败'
              break
            default:
              break
          }
        },
        clientId,
        controller.signal,
        sentFiles.map((item) => item.name),
        sentContexts.map((item) => item.blockId),
      )

      if (streamError) throw new Error(streamError)

      // 服务端没发 done 就断流时，把已收到的内容当作完成
      flush()
      patch(id, (t) => (t.status === 'done' ? t : { ...t, status: 'done' }))
      tourBus.emit('reader:chat-answered', { question, paperId })
    } catch (e) {
      flush()
      if (controller.signal.aborted) {
        patch(id, (t) => ({ ...t, status: 'stopped' }))
      } else {
        patch(id, (t) => ({
          ...t,
          status: 'error',
          error: e instanceof Error ? e.message : '未知错误',
        }))
      }
    } finally {
      if (timer !== null) window.clearTimeout(timer)
      abortRef.current = null
      busyRef.current = false
      setBusy(false)
    }
  }

  const submit = () => {
    const q = input.trim()
    if (!q || busyRef.current) return
    setInput('')
    void ask(q)
  }

  // 深链 ?ask=xxx 自动提问（也方便分享一个问题）；等历史恢复完再发，避免和恢复互相插队
  const asked = useRef(false)
  useEffect(() => {
    if (!historyReady || asked.current) return
    const q = new URLSearchParams(window.location.search).get('ask')
    if (!q) return
    asked.current = true
    void ask(q)
    // 触发后立刻把 ?ask= 从地址栏去掉：切标签时组件会重挂载，不清掉就会再问一遍，
    // 新 client id 让服务端也无法去重，等于多落一轮问答。
    const url = new URL(window.location.href)
    url.searchParams.delete('ask')
    window.history.replaceState({}, '', url)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyReady])

  return (
    <div
      className={`chat${dragging ? ' is-dragging' : ''}`}
      data-tour-chat={
        turns.some((turn) => turn.status === 'done' && turn.answer)
          ? 'ready'
          : 'pending'
      }
      onDragOver={(event) => {
        if (!canUpload) return
        event.preventDefault()
        setDragging(true)
      }}
      onDragLeave={(event) => {
        if (event.currentTarget.contains(event.relatedTarget as Node)) return
        setDragging(false)
      }}
      onDrop={(event) => {
        if (!canUpload) return
        event.preventDefault()
        setDragging(false)
        void addFiles(Array.from(event.dataTransfer.files))
      }}
    >
      <div className="chat-list" ref={listRef} onScroll={onScroll}>
        {!historyReady && turns.length === 0 && (
          <div className="turn-status">
            <span className="turn-dot" />
            正在恢复上次的对话…
          </div>
        )}

        {turns.length === 0 && historyReady && (
          <>
            <p className="greeting">Hi，准备好阅读了吗？你想从哪里开始？</p>
            <div className="quick-label">
              <IconSpark /> 快速开始
            </div>
            <div className="chips">
              {QUICK.map((q) => (
                <button
                  key={q.key}
                  className="chip"
                  onClick={() => void ask(q.label, q.key)}
                >
                  {q.label}
                </button>
              ))}
            </div>
            {!current && (
              <button className="chat-setup" onClick={onOpenSettings}>
                先配置回答模型，才能基于论文回答 →
              </button>
            )}
          </>
        )}

        {turns.map((t) => (
          <article className="turn" key={t.id}>
            <div className="turn-q">{t.question}</div>

            {t.attachments && t.attachments.length > 0 && (
              <div className="turn-attachments">
                {t.attachments.map((file) => (
                  <a
                    key={file.name}
                    className="attachment-chip"
                    href={file.url}
                    target="_blank"
                    rel="noreferrer"
                    title={`预览 ${file.name}`}
                  >
                    <span className="attachment-name">{file.name}</span>
                    <span className="attachment-size">{sizeText(file.size)}</span>
                  </a>
                ))}
              </div>
            )}

            {t.contexts && t.contexts.length > 0 && (
              <div className="turn-contexts">
                <span className="turn-contexts-label">附录</span>
                {t.contexts.map((item) => (
                  <span className="context-chip" key={item.id} title={item.caption || item.label}>
                    {item.kind === 'equation' ? 'TeX · ' : ''}
                    {item.label}
                  </span>
                ))}
              </div>
            )}

            <div
              className="turn-block"
              data-running={t.status === 'retrieving' || t.status === 'streaming'}
              data-failed={t.status === 'error'}
            >
              {t.tools.length > 0 && (
                <div className="turn-tools">
                  {t.tools.map((tool) => (
                    <ReaderToolCard key={tool.callId} tool={tool} onJump={onJump} />
                  ))}
                </div>
              )}

              {t.answer && (
                <div className="turn-text">
                  {t.demo ? (
                    <div className="turn-plain">{t.answer}</div>
                  ) : (
                    <Markdown
                      text={
                        t.status === 'streaming'
                          ? `${balanceMarkdown(t.answer)} ▍`
                          : t.status === 'stopped'
                            ? balanceMarkdown(t.answer)
                            : t.answer
                      }
                    />
                  )}
                </div>
              )}

              {(t.status === 'retrieving' || t.status === 'streaming') &&
                !t.answer && (
                  <p className="turn-pending">
                    {t.status === 'retrieving'
                      ? useWeb
                        ? '正在检索论文并联网搜索…'
                        : '正在检索论文…'
                      : '正在生成回答…'}
                  </p>
                )}

              {t.status === 'error' && (
                <p className="turn-error">
                  {t.error ? `回答中断：${t.error}` : '回答中断'}
                </p>
              )}

              {t.status === 'stopped' && (
                <footer className="turn-foot">
                  <span>已停止</span>
                  {t.tools.length > 0 && <em>· {t.tools.length} 次工具调用</em>}
                </footer>
              )}

              {t.status === 'done' && (
                <footer className="turn-foot">
                  <span>已完成</span>
                  {t.latency !== undefined && t.latency > 0 && (
                    <em>· 用时 {t.latency.toFixed(1)}s</em>
                  )}
                  {t.tools.length > 0 && (
                    <em>· {t.tools.length} 次工具调用</em>
                  )}
                  <em>{webMetaText(t.webStatus, t.usedWeb)}</em>
                </footer>
              )}
            </div>
          </article>
        ))}
      </div>

      <div className="composer">
        {contextItems.length > 0 && (
          <div className="chat-appendix">
            <div className="chat-appendix-head">
              <span>附录 · {contextItems.length}</span>
              <button
                type="button"
                className="chat-appendix-clear"
                onClick={() => onClearContext?.()}
              >
                清空
              </button>
            </div>
            <div className="chat-appendix-list">
              {contextItems.map((item) => (
                <div
                  className="appendix-item"
                  key={item.id}
                  title={item.caption || item.label}
                >
                  {item.kind === 'equation' ? (
                    <div className="appendix-eq">
                      {item.latex ? (
                        <Katex tex={item.latex} display={false} />
                      ) : (
                        <span>TeX</span>
                      )}
                    </div>
                  ) : item.imageUrl ? (
                    <img
                      className="appendix-thumb"
                      src={item.imageUrl}
                      alt={item.label}
                      loading="lazy"
                    />
                  ) : null}
                  <div className="appendix-meta">
                    <b>{item.label}</b>
                    <span>P{item.page}</span>
                  </div>
                  <button
                    type="button"
                    className="appendix-remove"
                    title="移出附录"
                    onClick={() => onRemoveContext?.(item.id)}
                  >
                    <IconClose />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}
        {(attachments.length > 0 || uploading || uploadError) && (
          <div className="chat-attachments">
            {attachments.map((file) => (
              <span className="attachment-chip" key={file.name}>
                <a
                  className="attachment-name"
                  href={file.url}
                  target="_blank"
                  rel="noreferrer"
                  title={`预览 ${file.name}`}
                >
                  {file.name}
                </a>
                <span className="attachment-size">{sizeText(file.size)}</span>
                <button
                  type="button"
                  className="attachment-remove"
                  title="移除附件"
                  onClick={() =>
                    setAttachments((prev) =>
                      prev.filter((one) => one.name !== file.name),
                    )
                  }
                >
                  <IconClose />
                </button>
              </span>
            ))}
            {uploading && <span className="chat-upload-note">正在上传附件…</span>}
            {uploadError && <span className="chat-upload-error">{uploadError}</span>}
          </div>
        )}
        <div className="composer-box">
          <textarea
            rows={1}
            value={input}
            placeholder={
              canUpload ? '问问这篇论文，可拖入文件或粘贴截图' : '问问这篇论文'
            }
            onChange={(e) => setInput(e.target.value)}
            onPaste={(event) => {
              const files = Array.from(event.clipboardData.files)
              if (files.length > 0 && canUpload) {
                event.preventDefault()
                void addFiles(files)
              }
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
          />
          {busy ? (
            <button
              className="send stop"
              onClick={() => abortRef.current?.abort()}
              title="停止生成"
            >
              <IconStop />
            </button>
          ) : (
            <button className="send" disabled={!input.trim()} onClick={submit} title="发送">
              <IconSend />
            </button>
          )}
        </div>
        <div className="composer-meta">
          {canUpload && (
            <div className="attach-picker" ref={libraryRef}>
              <input
                ref={fileInput}
                type="file"
                multiple
                hidden
                onChange={(event) => {
                  void addFiles(Array.from(event.target.files ?? []))
                  event.target.value = ''
                }}
              />
              <button
                className="chat-attach"
                type="button"
                title="上传附件（支持多选、拖拽、粘贴）"
                disabled={uploading}
                onClick={() => fileInput.current?.click()}
              >
                <IconPlus />
                附件
              </button>
              <button
                className="chat-attach"
                type="button"
                title="从这篇论文的附件库里勾选"
                aria-haspopup="menu"
                aria-expanded={libraryOpen}
                onClick={() => setLibraryOpen((v) => !v)}
              >
                <IconFolder />
                附件库
              </button>
              {libraryOpen && (
                <div className="attach-library" role="menu">
                  <div className="attach-library-head">论文附件库</div>
                  {libraryLoading ? (
                    <div className="attach-library-empty">正在读取…</div>
                  ) : libraryItems.length === 0 ? (
                    <div className="attach-library-empty">
                      还没有附件，先上传一个。
                    </div>
                  ) : (
                    <ul className="attach-library-list">
                      {libraryItems.map((item) => {
                        const picked = attachments.some(
                          (one) => one.name === item.name,
                        )
                        return (
                          <li key={item.name}>
                            <button
                              className="attach-library-item"
                              type="button"
                              data-picked={picked}
                              role="menuitemcheckbox"
                              aria-checked={picked}
                              title={item.name}
                              onClick={() => toggleLibraryFile(item)}
                            >
                              <span className="attach-library-check">
                                {picked && <IconCheck />}
                              </span>
                              <span className="attach-library-name">
                                {item.name}
                              </span>
                              <span className="attach-library-size">
                                {sizeText(item.size)}
                              </span>
                            </button>
                          </li>
                        )
                      })}
                    </ul>
                  )}
                  <div className="attach-library-foot">
                    勾选后随问题一起引用，下次提问可继续复用。
                  </div>
                </div>
              )}
            </div>
          )}
          <div className="model-picker" ref={pickerRef}>
            {current ? (
              <>
                <button
                  className="model-select"
                  onClick={() => setPickerOpen((v) => !v)}
                  title="切换回答模型"
                  aria-haspopup="menu"
                  aria-expanded={pickerOpen}
                >
                  <span className="model-select-dot" />
                  <span className="model-select-name">{current.label}</span>
                  <span className="model-select-caret" aria-hidden>
                    ▾
                  </span>
                </button>
                {pickerOpen && (
                  <div className="model-menu" role="menu">
                    <div className="model-menu-head">回答模型</div>
                    {modelOptions.map((m) => (
                      <button
                        key={m.id}
                        className="model-menu-item"
                        data-active={m.id === current.id}
                        role="menuitem"
                        onClick={() => chooseModel(m.id)}
                      >
                        <span className="model-menu-main">
                          <b>{m.label}</b>
                          <code>{m.model}</code>
                        </span>
                        {m.id === current.id && <IconCheck />}
                      </button>
                    ))}
                    <button
                      className="model-menu-manage"
                      onClick={() => {
                        setPickerOpen(false)
                        onOpenSettings?.()
                      }}
                    >
                      <IconSettings /> 管理模型…
                    </button>
                  </div>
                )}
              </>
            ) : (
              <button className="model-select is-empty" onClick={onOpenSettings}>
                <IconSpark /> 配置回答模型
              </button>
            )}
          </div>
          <label
            className="web-toggle"
            title="论文里没有写的内容，会尝试从公开网络检索后回答"
          >
            <input
              type="checkbox"
              checked={useWeb}
              onChange={(e) => setUseWeb(e.target.checked)}
            />
            允许联网检索
          </label>
        </div>
      </div>
    </div>
  )
}
