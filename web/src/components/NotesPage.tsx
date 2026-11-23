import { useEffect, useMemo, useState, type ComponentType } from 'react'
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
  IconCuboid,
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
  type CatalogCard,
  type CatalogFieldItem,
  type CatalogItem,
  type CatalogOverview,
  type FieldNote,
  type FieldTheme,
  type NoteDetail,
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

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请重试'
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

  // 目录首次构建需要拉一次文件树；构建期间每 3 秒回看一次，就绪后停下。
  useEffect(() => {
    const state = overview?.status.state
    if (!state || state === 'ready') return
    const timer = window.setTimeout(() => void loadOverview(), 3000)
    return () => window.clearTimeout(timer)
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
      setItems((prev) => (offset > 0 ? [...prev, ...result.items] : result.items))
    } catch (error) {
      notify(errorText(error), 'err')
    } finally {
      setLoading(false)
    }
  }

  const fillDescriptions = async (rows: FieldNote[]) => {
    const missing = rows.filter((row) => !row.description).map((row) => row.path)
    const chunks: string[][] = []
    for (let index = 0; index < missing.length; index += 10) {
      chunks.push(missing.slice(index, index + 10))
    }
    // 各批并发，避免 30 篇串行等待把摘要拖到十几秒。
    await Promise.all(
      chunks.map(async (chunk) => {
        try {
          const map = await fetchDescriptions(chunk)
          setFieldItems((prev) =>
            prev.map((item) =>
              map[item.path] ? { ...item, description: map[item.path] } : item,
            ),
          )
        } catch {
          // 摘要失败不影响列表展示，保留骨架
        }
      }),
    )
  }

  const loadField = async (offset = 0) => {
    if (!conference || !field) return
    setFieldLoading(true)
    try {
      const result = await fetchFieldNotes(conference, field, 30, offset)
      setFieldItems((prev) => (offset > 0 ? [...prev, ...result.items] : result.items))
      setMatched(result.matched)
      setAcross(result.across)
      setThemes(result.themes)
      void fillDescriptions(result.items)
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
    setMode('list')
    setFilterUrl(card.name, item.name)
  }

  const switchConference = (name: string) => {
    setConference(name)
    setQuery('')
    setFieldItems([])
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

  const heading = useMemo(() => {
    if (query.trim()) return `“${query.trim()}” 的搜索结果`
    if (field) return `${conference} · ${fieldLabel || field}`
    return conference || '全部笔记'
  }, [query, field, fieldLabel, conference])

  const state = overview?.status.state
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
            <div className="notes-detail">
              {detail.blocks.length === 0 && detailLoading ? (
                <div className="notes-state"><IconLoader size={13} className="notes-spin" />正在加载笔记正文…</div>
              ) : (
                <Markdown text={blocksToMarkdown(detail.blocks)} breaks={false} />
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
                placeholder="搜索标题、领域或关键词"
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
              <div className="notes-state">
                {overview?.total ? `正在更新目录（已有 ${overview.total} 篇）…` : '正在构建笔记目录…'}
              </div>
            )}
            {state === 'error' && (
              <div className="notes-state notes-state-error">
                目录暂时不可用：{overview?.status.error || '请检查网络'}
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
                placeholder="在当前结果中搜索"
                onChange={(event) => setQuery(event.target.value)}
              />
              {query && (
                <button className="notes-icon-btn" title="清除搜索" onClick={() => setQuery('')}>
                  <IconClose />
                </button>
              )}
            </div>

            {notice && <div className={'library-notice ' + notice.kind}>{notice.text}</div>}
            {detailLoading && <div className="notes-state">正在加载笔记…</div>}

            {items.length === 0 && !loading ? (
              <div className="library-empty notes-empty"><IconInbox size={18} />没有匹配的笔记。</div>
            ) : (
              <div className="notes-grid">
                {items.map((item) => (
                  <button
                    key={item.path}
                    className="notes-card"
                    onClick={() => void openNote(item)}
                  >
                    <div className="notes-card-meta">
                      <span className="notes-chip">{item.conference}</span>
                      <span className="notes-field">{item.label || item.field}</span>
                    </div>
                    <strong>{item.title}</strong>
                  </button>
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