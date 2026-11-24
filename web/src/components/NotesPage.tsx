import { useEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from 'react'
import { Markdown } from './Markdown'
import { SettingsDialog } from './SettingsDialog'
import { Sidebar, type NavKey } from './Sidebar'
import {
  IconAperture,
  IconBack,
  IconBook,
  IconBot,
  IconBrainCircuit,
  IconCalendarDays,
  IconCamera,
  IconChevronDown,
  IconClose,
  IconCode,
  IconCuboid,
  IconExternalLink,
  IconFileText,
  IconGlobe,
  IconHash,
  IconImages,
  IconInbox,
  IconLanguages,
  IconLayers,
  IconLightbulb,
  IconLoader,
  IconMicroscope,
  IconNetwork,
  IconPdf,
  IconRefresh,
  IconSearch,
  IconShieldCheck,
  IconSigma,
  IconTag,
  IconTarget,
} from './icons'
import {
  blocksToMarkdown,
  fetchCatalog,
  fetchDescriptions,
  fetchFieldNotes,
  fetchOnlineNote,
  fetchOverview,
  getNote,
  importNotePaper,
  refreshCatalog,
  splitNoteMeta,
  type CatalogCard,
  type CatalogFieldItem,
  type CatalogItem,
  type CatalogOverview,
  type CatalogQueryPlan,
  type FieldNote,
  type FieldTheme,
  type NoteDetail,
  type NoteMetaItem,
} from '../lib/notes'

const PAGE_SIZE = 60
const CHIP_LIMIT = 5

type IconComponent = ComponentType<{ size?: number; className?: string }>

const CONFERENCE_ICONS: Record<string, IconComponent> = {
  CVPR: IconCamera,
  ICCV: IconAperture,
  ECCV: IconAperture,
  ICLR: IconLightbulb,
  ICML: IconBrainCircuit,
  NeurIPS: IconNetwork,
  ACL: IconLanguages,
  AAAI: IconBot,
}

const GROUP_ICONS: Record<string, IconComponent> = {
  'LLM 与大模型': IconBrainCircuit,
  '生成与多模态': IconImages,
  '视觉与 3D': IconCuboid,
  '安全与可信': IconShieldCheck,
  '强化学习与决策': IconTarget,
  '基础与理论': IconSigma,
  '应用与交叉': IconMicroscope,
}

function conferenceIcon(name: string): IconComponent {
  return CONFERENCE_ICONS[name.replace(/\d{4}$/, '')] || IconBook
}

function groupIcon(name: string): IconComponent {
  return GROUP_ICONS[name] || IconLayers
}

const META_ICONS: Record<string, IconComponent> = {
  '会议': IconCalendarDays,
  conference: IconCalendarDays,
  arxiv: IconHash,
  '代码': IconCode,
  code: IconCode,
  github: IconCode,
  '领域': IconLayers,
  field: IconLayers,
  '关键词': IconTag,
  '关键字': IconTag,
  keyword: IconTag,
  keywords: IconTag,
  '论文链接': IconExternalLink,
  '原文链接': IconExternalLink,
  '链接': IconExternalLink,
  link: IconExternalLink,
  url: IconExternalLink,
}

function metaIcon(label: string): IconComponent {
  return META_ICONS[label.trim().toLowerCase()] ?? IconHash
}

const KEYWORD_LABELS = new Set(['关键词', '关键字', 'keyword', 'keywords'])
const CODE_LABELS = new Set(['代码', 'code', 'github'])

function metaKind(label: string): 'code' | 'keyword' | 'text' {
  const key = label.trim().toLowerCase()
  if (CODE_LABELS.has(key)) return 'code'
  if (KEYWORD_LABELS.has(key)) return 'keyword'
  return 'text'
}

const META_LINK = /\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s<>()]+)/g

/** 仓库地址去掉协议与前缀斜杠，保留可读的主机 + 路径。 */
function shortLink(href: string): string {
  try {
    const url = new URL(href)
    return `${url.host}${url.pathname.replace(/\/+$/, '')}`
  } catch {
    return href
  }
}

/** 元信息值里可能有 markdown 链接或裸地址，渲染成可点链接。 */
function renderMetaValue(value: string, kind: 'code' | 'keyword' | 'text'): ReactNode {
  const source =
    kind === 'keyword' ? value.replace(/\s*[,，、;；]\s*/g, '，').trim() : value
  const nodes: ReactNode[] = []
  let last = 0
  for (const match of source.matchAll(META_LINK)) {
    const index = match.index ?? 0
    if (index > last) nodes.push(source.slice(last, index))
    const href = match[2] ?? match[3]
    nodes.push(
      <a key={`${index}-${href}`} href={href} title={href} target="_blank" rel="noreferrer">
        {match[2] ? match[1] || shortLink(href) : shortLink(href)}
      </a>,
    )
    last = index + match[0].length
  }
  if (last < source.length) nodes.push(source.slice(last))
  return nodes.length > 0 ? nodes : source
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请重试'
}

const CJK_RE = /[\u3400-\u9fff]/

function escapePattern(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** 把命中的词在文本里的位置找出来：英文按整词（含空格/连字符变体），中文按子串。 */
function hitRanges(text: string, terms: string[]): [number, number][] {
  if (!text || !terms.length) return []
  const ranges: [number, number][] = []
  for (const term of terms) {
    const value = term.trim()
    if (!value) continue
    if (CJK_RE.test(value)) {
      let from = 0
      while (from <= text.length) {
        const at = text.indexOf(value, from)
        if (at < 0) break
        ranges.push([at, at + value.length])
        from = at + value.length
      }
      continue
    }
    // 单个字母/数字不参与高亮，和“字母不算命中”保持一致。
    if (value.length < 2) continue
    const pattern = escapePattern(value).replace(/\s+/g, '[-_\\s]+')
    const regex = new RegExp(`(^|[^a-z0-9])(${pattern})(?![a-z0-9])`, 'gi')
    let match: RegExpExecArray | null
    while ((match = regex.exec(text))) {
      const start = (match.index ?? 0) + (match[1]?.length ?? 0)
      const length = match[2]?.length ?? 0
      if (!length) {
        regex.lastIndex += 1
        continue
      }
      ranges.push([start, start + length])
    }
  }
  ranges.sort((left, right) => left[0] - right[0] || right[1] - left[1])
  const merged: [number, number][] = []
  for (const [start, end] of ranges) {
    const last = merged[merged.length - 1]
    if (last && start <= last[1]) last[1] = Math.max(last[1], end)
    else merged.push([start, end])
  }
  return merged
}

function HighlightText({ text, terms }: { text: string; terms: string[] }) {
  // 先把词表拼成稳定 key：父组件每次渲染都会新建数组，直接用它当依赖会反复重算。
  const termsKey = terms.join('###')
  const ranges = useMemo(
    () => hitRanges(text, termsKey ? termsKey.split('###') : []),
    [text, termsKey],
  )
  if (!ranges.length) return <>{text}</>
  const nodes: ReactNode[] = []
  let cursor = 0
  ranges.forEach(([start, end], index) => {
    if (start > cursor) nodes.push(text.slice(cursor, start))
    nodes.push(
      <mark className="notes-hit" key={`hit-${start}-${index}`}>
        {text.slice(start, end)}
      </mark>,
    )
    cursor = end
  })
  if (cursor < text.length) nodes.push(text.slice(cursor))
  return <>{nodes}</>
}

/** 头部展示“中文 → 英文候选”，让用户知道为什么这些英文笔记会被召回。 */
function QueryPlan({ plan }: { plan: CatalogQueryPlan }) {
  const terms = useMemo(
    () =>
      plan.terms
        .map((item) => item.term)
        .filter((term) => term.toLowerCase() !== plan.raw.toLowerCase()),
    [plan],
  )
  if (!terms.length) return null
  return (
    <div className="notes-query-plan" title={terms.join(' · ')}>
      <IconLanguages size={12} />
      <span>
        <b>{plan.raw}</b> 已扩展为 {terms.slice(0, 8).join(' · ')}
        {terms.length > 8 ? ` 等 ${terms.length} 个词` : ''}
      </span>
    </div>
  )
}

interface Props {
  onNav: (key: NavKey) => void
  onOpenPaper?: (id: string) => void
}

export function NotesPage({ onNav, onOpenPaper }: Props) {
  const initial = new URLSearchParams(window.location.search)
  const [overview, setOverview] = useState<CatalogOverview | null>(null)
  const [mode, setMode] = useState<'overview' | 'list'>(
    initial.get('conference') || initial.get('field') ? 'list' : 'overview',
  )
  const [conference, setConference] = useState(initial.get('conference') || '')
  const [field, setField] = useState(initial.get('field') || '')
  const [fieldLabel, setFieldLabel] = useState('')
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<CatalogItem[]>([])
  const [fieldItems, setFieldItems] = useState<FieldNote[]>([])
  const [across, setAcross] = useState<{ name: string; count: number }[]>([])
  const [themes, setThemes] = useState<FieldTheme[]>([])
  const [fieldLoading, setFieldLoading] = useState(false)
  const [matched, setMatched] = useState(0)
  const [plan, setPlan] = useState<CatalogQueryPlan | null>(null)
  const [loading, setLoading] = useState(false)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [detail, setDetail] = useState<NoteDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [paperBusy, setPaperBusy] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [overviewError, setOverviewError] = useState('')
  const [notice, setNotice] = useState<{ text: string; kind: 'ok' | 'err' } | null>(null)

  const notify = (text: string, kind: 'ok' | 'err' = 'ok') => {
    setNotice({ text, kind })
    window.setTimeout(() => setNotice(null), 4200)
  }

  const loadOverview = async () => {
    try {
      const data = await fetchOverview()
      setOverview(data)
      setOverviewError('')
    } catch (error) {
      setOverviewError(errorText(error))
    }
  }

  useEffect(() => {
    void loadOverview()
  }, [])

  // URL 深链只带 field 名，目录就绪后补回可读标签。
  useEffect(() => {
    if (!field || fieldLabel || !overview) return
    const card = overview.conferences.find((item) => item.name === conference)
    const found = card?.groups.flatMap((group) => group.fields).find((item) => item.name === field)
    if (found) setFieldLabel(found.label)
  }, [field, fieldLabel, overview, conference])

  // 目录首次构建要拉文件树 + 预热检索索引；构建期间 1.5 秒回看一次进度。
  useEffect(() => {
    const state = overview?.status.state
    if (!state || state === 'ready') return
    const timer = window.setTimeout(() => void loadOverview(), 1500)
    return () => window.clearTimeout(timer)
  }, [overview?.status.state])

  // 目录构建完成后自动刷新当前视图：用户不用离开/切回广场才看到新内容。
  const lastCatalogState = useRef('')
  useEffect(() => {
    const state = overview?.status.state
    if (!state) return
    const previous = lastCatalogState.current
    lastCatalogState.current = state
    if (previous && previous !== 'ready' && state === 'ready' && mode === 'list') {
      if (field) void loadField(0)
      else void reload(0)
    }
    // 目录状态变化才需要补刷一次；筛选变化由上面的加载 effect 负责。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overview?.status.state])

  const reload = async (offset = 0) => {
    setLoading(true)
    try {
      const result = await fetchCatalog({
        conference,
        field,
        q: query.trim(),
        limit: PAGE_SIZE,
        offset,
      })
      setMatched(result.matched)
      setPlan(result.query ?? null)
      setItems((prev) => (offset > 0 ? [...prev, ...result.items] : result.items))
      void fillDescriptions(result.items, (map) =>
        setItems((prev) =>
          prev.map((item) =>
            map[item.path] ? { ...item, description: map[item.path] } : item,
          ),
        ),
      )
    } catch (error) {
      notify(errorText(error), 'err')
    } finally {
      setLoading(false)
    }
  }

  const fillDescriptions = async (
    rows: { path: string; description?: string }[],
    apply: (map: Record<string, string>) => void,
  ) => {
    const missing = rows.filter((row) => !row.description).map((row) => row.path)
    if (!missing.length) return
    // 后端一次最多取 30 条；整页 60 篇拆两批并发，每批仍只起一条 curl。
    const chunks: string[][] = []
    for (let index = 0; index < missing.length; index += 30) {
      chunks.push(missing.slice(index, index + 30))
    }
    try {
      const maps = await Promise.all(
        chunks.map((chunk) => fetchDescriptions(chunk).catch(() => ({}))),
      )
      apply(Object.assign({}, ...maps) as Record<string, string>)
    } catch {
      // 摘要失败不影响列表展示，保留骨架
    }
  }

  const loadField = async (offset = 0) => {
    if (!conference || !field) return
    setFieldLoading(true)
    try {
      const result = await fetchFieldNotes(conference, field, 30, offset)
      setFieldItems((prev) => (offset > 0 ? [...prev, ...result.items] : result.items))
      setMatched(result.matched)
      setPlan(null)
      setAcross(result.across)
      setThemes(result.themes)
      void fillDescriptions(result.items, (map) =>
        setFieldItems((prev) =>
          prev.map((item) =>
            map[item.path] ? { ...item, description: map[item.path] } : item,
          ),
        ),
      )
    } catch (error) {
      notify(errorText(error), 'err')
    } finally {
      setFieldLoading(false)
    }
  }

  useEffect(() => {
    if (mode !== 'list') return
    if (field) {
      void loadField(0)
      return
    }
    const timer = window.setTimeout(() => void reload(0), query ? 320 : 0)
    return () => window.clearTimeout(timer)
  }, [mode, conference, field, query])

  useEffect(() => {
    const noteId = new URLSearchParams(window.location.search).get('note')
    if (!noteId) return
    void getNote(noteId)
      .then(setDetail)
      .catch(() => undefined)
  }, [])

  const setFilterUrl = (conf: string, fieldName: string) => {
    const url = new URL(window.location.href)
    if (conf) url.searchParams.set('conference', conf)
    else url.searchParams.delete('conference')
    if (fieldName) url.searchParams.set('field', fieldName)
    else url.searchParams.delete('field')
    window.history.replaceState({}, '', url)
  }

  const openConference = (name: string) => {
    const card = overview?.conferences.find((item) => item.name === name)
    const first = card?.groups[0]?.fields[0]
    setQuery('')
    setExpanded({})
    setItems([])
    setFieldItems([])
    setPlan(null)
    setMode('list')
    if (card && first) {
      setMatched(first.count)
      setConference(name)
      setField(first.name)
      setFieldLabel(first.label)
      setFilterUrl(name, first.name)
      return
    }
    setConference(name)
    setField('')
    setFieldLabel('')
    setFilterUrl(name, '')
  }

  const openField = (card: CatalogCard, item: CatalogFieldItem) => {
    setMatched(item.count)
    setConference(card.name)
    setField(item.name)
    setFieldLabel(item.label)
    setQuery('')
    setItems([])
    setFieldItems([])
    setPlan(null)
    setMode('list')
    setFilterUrl(card.name, item.name)
  }

  const switchConference = (name: string) => {
    setConference(name)
    setQuery('')
    setFieldItems([])
    setPlan(null)
    setFilterUrl(name, field)
  }

  const backToOverview = () => {
    setFilterUrl('', '')
    setMode('overview')
    setConference('')
    setField('')
    setFieldLabel('')
    setQuery('')
    setItems([])
    setMatched(0)
    setPlan(null)
  }

  // 深链只带会议时，自动落到该会议的第一个领域，与点会议名保持一致。
  useEffect(() => {
    if (mode !== 'list' || field || !conference || !overview) return
    const card = overview.conferences.find((item) => item.name === conference)
    const first = card?.groups[0]?.fields[0]
    if (first) {
      setMatched(first.count)
      setField(first.name)
      setFieldLabel(first.label)
      setFilterUrl(conference, first.name)
    }
  }, [mode, field, conference, overview])

  const openNote = async (item: CatalogItem) => {
    setDetailLoading(true)
    // 先切到详情页显示标题与骨架，网络回来再替换，避免点完停在列表上等待。
    setDetail({
      id: '',
      title: item.title,
      conference: item.conference,
      field: item.field,
      tags: [],
      description: '',
      source_path: item.path.replace(/^docs\//, ''),
      blocks: [],
      sections: {},
    })
    try {
      const result = await fetchOnlineNote(item.path)
      setDetail(result.note)
      const url = new URL(window.location.href)
      url.searchParams.set('note', result.note.id)
      window.history.replaceState({}, '', url)
    } catch (error) {
      setDetail(null)
      notify(errorText(error), 'err')
    } finally {
      setDetailLoading(false)
    }
  }

  const closeNote = () => {
    setDetail(null)
    const url = new URL(window.location.href)
    url.searchParams.delete('note')
    window.history.replaceState({}, '', url)
  }

  const doRefreshCatalog = async () => {
    try {
      await refreshCatalog()
      notify('已开始刷新笔记目录')
      setOverview((prev) => (prev ? { ...prev, status: { ...prev.status, state: 'loading' } } : prev))
    } catch (error) {
      notify(errorText(error), 'err')
    }
  }

  const doImportPaper = async () => {
    if (!detail) return
    setPaperBusy(true)
    try {
      const result = await importNotePaper(detail.id)
      setDetail({ ...detail, paper_id: result.paper.id })
      notify(
        result.created ? '论文已下载并加入文献库，正在后台解析' : '这篇论文已在文献库中，已直接关联',
      )
    } catch (error) {
      notify(errorText(error), 'err')
    } finally {
      setPaperBusy(false)
    }
  }

  const activeCard = useMemo(
    () => overview?.conferences.find((item) => item.name === conference) ?? null,
    [overview, conference],
  )

  const noteMeta = useMemo(() => splitNoteMeta(detail?.blocks ?? []), [detail])

  // 标题里高亮本次实际命中的英文候选；摘要里补上查询的中文原词。
  const queryTerms = useMemo(() => plan?.concepts ?? [], [plan])
  const termsFor = (item: CatalogItem) =>
    Array.from(new Set([...(item.hits ?? []), ...queryTerms]))

  // 页头已经显示标题，正文里解析出的 title 块不再重复渲染。
  const noteBody = useMemo(
    () => blocksToMarkdown(noteMeta.rest.filter((block) => block.kind !== 'title')),
    [noteMeta],
  )

  const heading = useMemo(() => {
    if (query.trim()) return `“${query.trim()}” 的搜索结果`
    if (field) return `${conference} · ${fieldLabel || field}`
    return conference || '全部笔记'
  }, [query, field, fieldLabel, conference])

  const state = overview?.status.state
  const catalogPercent = Math.max(0, Math.min(100, overview?.status.percent ?? 0))
  const phase = overview?.status.phase || ''
  const catalogPhaseText =
    phase === 'fetching'
      ? '正在获取笔记目录'
      : phase === 'indexing'
        ? '正在建立检索索引'
        : overview?.total
          ? '正在更新笔记目录'
          : '正在构建笔记目录'
  const busyState = state === 'loading' || state === 'idle'

  return (
    <div className="library">
      <Sidebar
        active="notes"
        onNav={onNav}
        onSettings={() => setSettingsOpen(true)}
        folderSection={<></>}
      />

      <main className="library-main">
        {detail ? (
          <div className="notes-reading">
            <header className="library-head">
              <div className="library-title">
                <button className="notes-icon-btn" onClick={closeNote} title="返回笔记广场">
                  <IconBack />
                </button>
                <h1>{detail.title}</h1>
              </div>
              <div className="notes-actions">
                <span className="notes-source">
                  {detail.conference || '未标注会议'} · {detail.field}
                </span>
                {detail.paper_id ? (
                  <button
                    className="notes-icon-btn"
                    title="打开论文"
                    onClick={() => onOpenPaper?.(detail.paper_id as string)}
                  >
                    <IconBook />
                  </button>
                ) : (
                  <button
                    className="notes-icon-btn"
                    title="导入 PDF 到文献库"
                    disabled={paperBusy}
                    onClick={() => void doImportPaper()}
                  >
                    <IconPdf />
                  </button>
                )}
              </div>
            </header>
            <p className="notes-license">
              内容来源 {detail.origin || 'paper-notes'}，许可 {detail.license || 'CC BY-NC-SA 4.0'}
            </p>
            {noteMeta.meta.length > 0 && (
              <dl className="notes-meta">
                {noteMeta.meta.map((item: NoteMetaItem) => {
                  const MetaIcon = metaIcon(item.label)
                  const kind = metaKind(item.label)
                  return (
                    <div
                      className="notes-meta-row"
                      data-kind={kind}
                      key={`${item.label}:${item.value}`}
                    >
                      <dt title={item.label} aria-label={item.label}>
                        <MetaIcon size={13} />
                      </dt>
                      <dd>{renderMetaValue(item.value, kind)}</dd>
                    </div>
                  )
                })}
              </dl>
            )}
            <div className="notes-detail">
              {detail.blocks.length === 0 && detailLoading ? (
                <div className="notes-state"><IconLoader size={13} className="notes-spin" />正在加载笔记正文…</div>
              ) : (
                <Markdown text={noteBody} breaks={false} />
              )}
            </div>
          </div>
        ) : mode === 'overview' ? (
          <>
            <header className="notes-hero">
              <div className="notes-hero-text">
                <h1>笔记广场</h1>
                <p>按会议与领域浏览 AI 顶会论文解读，点开即读。</p>
              </div>
              <div className="notes-stats">
                <div className="notes-stat">
                  <IconBook />
                  <div className="notes-stat-body">
                    <strong>{overview?.total ?? '—'}</strong>
                    <span>篇笔记</span>
                  </div>
                </div>
                <div className="notes-stat">
                  <IconCalendarDays size={15} />
                  <div className="notes-stat-body">
                    <strong>{overview?.conferenceCount ?? '—'}</strong>
                    <span>个会议</span>
                  </div>
                </div>
                <div className="notes-stat">
                  <IconLayers size={15} />
                  <div className="notes-stat-body">
                    <strong>{overview?.fieldCount ?? '—'}</strong>
                    <span>个领域</span>
                  </div>
                </div>
                <button
                  className="notes-icon-btn"
                  title="刷新目录"
                  onClick={() => void doRefreshCatalog()}
                >
                  <IconRefresh />
                </button>
              </div>
            </header>

            <div className="notes-search">
              <IconSearch />
              <input
                value={query}
                placeholder="搜索标题、领域或关键词（支持中文）"
                onChange={(event) => {
                  setQuery(event.target.value)
                  if (event.target.value.trim()) setMode('list')
                }}
              />
              {query && (
                <button
                  className="notes-icon-btn"
                  title="清除搜索"
                  onClick={() => {
                    setQuery('')
                    setMode('overview')
                  }}
                >
                  <IconClose />
                </button>
              )}
            </div>

            {notice && <div className={'library-notice ' + notice.kind}>{notice.text}</div>}

            {busyState && (
              <div className="notes-state notes-building" role="status" aria-live="polite">
                <span className="notes-building-text">
                  {catalogPhaseText}
                  {overview?.total ? `（已有 ${overview.total} 篇）` : ''}
                  {catalogPercent > 0 ? ` · ${catalogPercent}%` : ''}
                </span>
                {catalogPercent > 0 && (
                  <div
                    className="notes-progress"
                    role="progressbar"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={catalogPercent}
                  >
                    <span style={{ width: `${catalogPercent}%` }} />
                  </div>
                )}
              </div>
            )}
            {state === 'error' && (
              <div className="notes-state notes-state-error">
                目录暂时不可用：{overview?.status.error || '请检查网络'}
              </div>
            )}
            {state === 'ready' && overview?.status.error && (
              <div className="notes-state notes-state-error">
                目录更新失败，正在使用上次缓存：{overview.status.error}
              </div>
            )}
            {overviewError && (
              <div className="notes-state notes-state-error">目录加载失败：{overviewError}</div>
            )}

            {overview && overview.conferences.length > 0 ? (
              <div className="notes-conf-grid">
                {overview.conferences.map((card) => {
                  const ConfIcon = conferenceIcon(card.name)
                  return (
                  <article key={card.name} className="notes-conf-card">
                    <header className="notes-conf-head">
                      <button
                        className="notes-conf-name"
                        onClick={() => openConference(card.name)}
                        title={`查看 ${card.name} 全部笔记`}
                      >
                        <ConfIcon size={15} />
                        <span>{card.name}</span>
                      </button>
                      <span className="notes-conf-meta">
                        {card.count.toLocaleString()} 篇 · {card.fieldCount} 领域
                      </span>
                    </header>

                    {card.groups.map((group) => {
                      const key = `${card.name}:${group.name}`
                      const open = expanded[key] === true
                      const fields = open ? group.fields : group.fields.slice(0, CHIP_LIMIT)
                      const rest = group.fields.length - fields.length
                      const GroupIcon = groupIcon(group.name)
                      return (
                        <section key={key} className="notes-group">
                          <h3>
                            <GroupIcon size={12} />
                            {group.name}
                          </h3>
                          <div className="notes-field-chips">
                            {fields.map((item) => (
                              <button
                                key={item.name}
                                className="notes-field-chip"
                                title={`${item.label} · ${item.count} 篇`}
                                onClick={() => openField(card, item)}
                              >
                                {item.label}
                                <em>{item.count}</em>
                              </button>
                            ))}
                            {(rest > 0 || open) && (
                              <button
                                className="notes-icon-btn notes-chip-toggle"
                                title={open ? '收起' : `展开其余 ${rest} 个领域`}
                                onClick={() =>
                                  setExpanded((prev) => ({ ...prev, [key]: !open }))
                                }
                              >
                                <IconChevronDown data-flip={open} />
                              </button>
                            )}
                          </div>
                        </section>
                      )
                    })}
                  </article>
                  )
                })}
              </div>
            ) : (
              !busyState &&
              state !== 'error' &&
              !overviewError && <div className="library-empty notes-empty"><IconInbox size={18} />还没有可浏览的笔记。</div>
            )}
          </>
        ) : field && activeCard ? (
          <div className="notes-field">
            <aside className="notes-nav">
              <button className="notes-nav-back" onClick={backToOverview} title="返回笔记广场">
                <IconBack />
                <span>{conference}</span>
              </button>
              {activeCard.groups.map((group) => {
                const GroupIcon = groupIcon(group.name)
                return (
                <section key={group.name} className="notes-nav-group">
                  <h4>
                    <GroupIcon size={12} />
                    {group.name}
                  </h4>
                  {group.fields.map((item) => (
                    <button
                      key={item.name}
                      className="notes-nav-item"
                      data-active={item.name === field}
                      onClick={() => openField(activeCard, item)}
                    >
                      <span>{item.label}</span>
                      <em>{item.count}</em>
                    </button>
                  ))}
                </section>
                )
              })}
            </aside>

            <section className="notes-field-main">
              <header className="notes-field-head">
                <h1>{fieldLabel || field}</h1>
                <span>
                  {conference} · {matched.toLocaleString()} 篇论文解读
                </span>
              </header>

              {across.length > 1 && (
                <div className="notes-across">
                  <span className="notes-across-label"><IconGlobe size={12} />同领域跨会议</span>
                  {across
                    .filter((item) => item.name !== conference)
                    .map((item) => (
                      <button key={item.name} onClick={() => switchConference(item.name)}>
                        {item.name} ({item.count})
                      </button>
                    ))}
                </div>
              )}

              {themes.length > 0 && (
                <div className="notes-themes">
                  <span className="notes-across-label"><IconHash size={12} />高频主题</span>
                  {themes.map((theme) => (
                    <span key={theme.word}>
                      {theme.word} ×{theme.count}
                    </span>
                  ))}
                </div>
              )}

              {notice && <div className={'library-notice ' + notice.kind}>{notice.text}</div>}
              {detailLoading && <div className="notes-state">正在加载笔记…</div>}

              {fieldItems.length === 0 && !fieldLoading ? (
                <div className="library-empty notes-empty"><IconInbox size={18} />该领域还没有可浏览的笔记。</div>
              ) : (
                <div className="notes-paper-list">
                  {fieldItems.map((item) => (
                    <article key={item.path} className="notes-paper">
                      <button className="notes-paper-title" onClick={() => void openNote(item)}>
                        <IconFileText size={13} />
                        <span>{item.title}</span>
                      </button>
                      {item.description ? (
                        <p>{item.description}</p>
                      ) : (
                        <div className="notes-paper-skeleton" />
                      )}
                    </article>
                  ))}
                </div>
              )}

              {fieldItems.length < matched && (
                <div className="notes-more">
                  <button
                    className="notes-icon-btn"
                    title="加载更多"
                    disabled={fieldLoading}
                    onClick={() => void loadField(fieldItems.length)}
                  >
                    <IconChevronDown />
                  </button>
                </div>
              )}

              {fieldLoading && fieldItems.length === 0 && (
                <div className="library-empty notes-empty"><IconLoader size={15} className="notes-spin" />正在加载论文摘要…</div>
              )}
            </section>
          </div>
        ) : (
          <>
            <header className="notes-list-head">
              <button className="notes-icon-btn" title="返回笔记广场" onClick={backToOverview}>
                <IconBack />
              </button>
              <div className="notes-list-title">
                <h1>{heading}</h1>
                <span>{matched.toLocaleString()} 篇</span>
              </div>
              <button
                className="notes-icon-btn"
                title="刷新"
                onClick={() => void reload(0)}
              >
                <IconRefresh />
              </button>
            </header>

            <div className="notes-search">
              <IconSearch />
              <input
                value={query}
                placeholder="在当前结果中搜索（支持中文）"
                onChange={(event) => setQuery(event.target.value)}
              />
              {query && (
                <button className="notes-icon-btn" title="清除搜索" onClick={() => setQuery('')}>
                  <IconClose />
                </button>
              )}
            </div>

            {plan && <QueryPlan plan={plan} />}

            {notice && <div className={'library-notice ' + notice.kind}>{notice.text}</div>}
            {detailLoading && <div className="notes-state">正在加载笔记…</div>}

            {items.length === 0 && !loading ? (
              <div className="library-empty notes-empty"><IconInbox size={18} />没有匹配的笔记。</div>
            ) : (
              <div className="notes-paper-list">
                {items.map((item) => (
                  <article key={item.path} className="notes-paper">
                    <button
                      className="notes-paper-title"
                      onClick={() => void openNote(item)}
                      title={item.title}
                    >
                      <IconFileText size={13} />
                      <span>
                        <HighlightText text={item.title} terms={termsFor(item)} />
                      </span>
                      <em className="notes-paper-tag">
                        <HighlightText
                          text={`${item.conference} · ${item.label || item.field}`}
                          terms={termsFor(item)}
                        />
                      </em>
                    </button>
                    {item.description ? (
                      <p>
                        <HighlightText text={item.description} terms={termsFor(item)} />
                      </p>
                    ) : (
                      <div className="notes-paper-skeleton" />
                    )}
                  </article>
                ))}
              </div>
            )}

            {items.length < matched && (
              <div className="notes-more">
                <button
                  className="notes-icon-btn"
                  title="加载更多"
                  disabled={loading}
                  onClick={() => void reload(items.length)}
                >
                  <IconChevronDown />
                </button>
              </div>
            )}

            {loading && items.length === 0 && <div className="library-empty notes-empty"><IconLoader size={15} className="notes-spin" />正在加载…</div>}
          </>
        )}
      </main>

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onLibraryChanged={() => undefined}
      />
    </div>
  )
}