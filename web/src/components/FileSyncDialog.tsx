import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import {
  getZoteroWebStatus,
  importZotero,
  listPapers,
  pushZoteroWeb,
  saveZoteroWebConfig,
  syncZotero,
  testZoteroWeb,
  zoteroItems,
  zoteroPdfUrl,
  zoteroStatus,
  type PaperRow,
  type ZoteroItem,
  type ZoteroStatus,
  type ZoteroWebReport,
  type ZoteroWebStatus,
} from '../lib/library'
import {
  formatSyncTime,
  listScholarReadPapers,
  loadScholarReadSettings,
  probeScholarRead,
  runScholarReadSync,
  saveScholarReadSettings,
  type ScholarReadPaper,
  type ScholarReadStatus,
  type ScholarReadSyncResult,
} from '../lib/scholareadSync'
import { isMobileShell, mediaUrl } from '../lib/mobile'
import zoteroIcon from '../assets/zotero.png'
import scholareadIcon from '../assets/scholaread.png'
import phoneIcon from '../assets/phone.svg'
import { PackageExportPanel, MobileDeviceSyncPanel } from './DeviceSync'
import {
  IconCheck,
  IconClose,
  IconDownload,
  IconExpand,
  IconRefresh,
  IconSearch,
  IconSend,
} from './icons'

/** Zotero 拉取前预览 PDF：与研究页复用同一个 pdf.js 查看器。 */
const PdfViewer = lazy(() =>
  import('../research/preview/PdfViewer').then((module) => ({
    default: module.PdfViewer,
  })),
)

export type FileSyncTarget = 'zotero' | 'scholaread' | 'phone'
type ZoteroDirection = 'pull' | 'push'
type ScholarDirection = 'pull' | 'push'

interface Props {
  open: boolean
  onClose: () => void
  onSynced: () => void
  initialTarget?: FileSyncTarget
}

type SyncTone = 'zotero' | 'scholaread' | 'phone'

const SYNC_META: Record<
  SyncTone,
  { mark: string; title: string; desc: string; icon?: string }
> = {
  zotero: {
    mark: 'Z',
    icon: zoteroIcon,
    title: 'Zotero 同步',
    desc: '查看 Zotero 里的论文，勾选后拉取到文献库；也可以把本地论文、分类与 PDF 推到 Zotero 云端。',
  },
  scholaread: {
    mark: 'S',
    icon: scholareadIcon,
    title: '靠岸学术同步',
    desc: '查看靠岸学术（Scholaread）里的论文，勾选后拉取到文献库；退出靠岸学术后可把阅读进度写回。',
  },
  phone: {
    mark: 'M',
    icon: phoneIcon,
    title: '手机同步',
    desc: '把文献打包成不依赖网络的 .paperead 文件，发到手机上导入离线库。',
  },
}

function errorText(error: unknown, fallback = '操作失败，请重试'): string {
  return error instanceof Error ? error.message : fallback
}

function syncTime(value: number): string {
  return value ? new Date(value * 1000).toLocaleString() : '尚未同步'
}

function SyncShell({
  open,
  onClose,
  tone,
  children,
}: {
  open: boolean
  onClose: () => void
  tone: SyncTone
  children: React.ReactNode
}) {
  if (!open) return null
  const meta = SYNC_META[tone]
  return (
    <div className="modal-mask" onMouseDown={onClose}>
      <div
        className={`modal sync-modal sync-modal--${tone}`}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal
        aria-label={meta.title}
      >
        <header className="sync-modal-head">
          <div className="sync-modal-title">
            <span
              className="sync-brand"
              data-tone={tone}
              data-image={meta.icon ? 'true' : undefined}
            >
              {meta.icon ? <img src={meta.icon} alt="" /> : meta.mark}
            </span>
            <div>
              <h2>{meta.title}</h2>
              <p>{meta.desc}</p>
            </div>
          </div>
          <button className="icon-btn" onClick={onClose} title="关闭">
            <IconClose />
          </button>
        </header>
        <div className="sync-modal-body">{children}</div>
      </div>
    </div>
  )
}

function CheckAll({
  ids,
  checked,
  disabled,
  onChange,
}: {
  ids: string[]
  checked: Set<string>
  disabled?: boolean
  onChange: (next: Set<string>) => void
}) {
  const all = ids.length > 0 && ids.every((id) => checked.has(id))
  return (
    <button
      type="button"
      className="mini-btn"
      disabled={disabled || ids.length === 0}
      onClick={() => onChange(all ? new Set() : new Set(ids))}
    >
      {all ? '取消全选' : '全选'}
    </button>
  )
}

function PaperCheckRow({
  title,
  subtitle,
  tag,
  checked,
  disabled,
  onToggle,
  onPreview,
  tone,
  coverUrl,
}: {
  title: string
  subtitle: string
  tag?: string
  checked: boolean
  disabled?: boolean
  onToggle: () => void
  onPreview?: () => void
  tone?: SyncTone
  coverUrl?: string
}) {
  const initial = (title || '?').trim().slice(0, 1).toUpperCase()
  return (
    <label
      className="sync-card"
      data-checked={checked || undefined}
      data-disabled={disabled || undefined}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={onToggle}
      />
      <div className="sync-card-cover" data-tone={tone || 'zotero'}>
        {coverUrl && (
          <img
            src={coverUrl}
            alt=""
            loading="lazy"
            onError={(event) => {
              event.currentTarget.style.display = 'none'
            }}
          />
        )}
        <span>{initial}</span>
        {tag && <em>{tag}</em>}
        {onPreview && (
          <button
            type="button"
            className="sync-card-preview"
            title="预览 PDF"
            onClick={(event) => {
              event.preventDefault()
              event.stopPropagation()
              onPreview()
            }}
          >
            <IconExpand />
          </button>
        )}
      </div>
      <div className="sync-card-info">
        <b title={title}>{title || '未命名论文'}</b>
        <span title={subtitle}>{subtitle || '作者未知'}</span>
      </div>
    </label>
  )
}

function SyncGuide({
  title,
  steps,
}: {
  title: string
  steps: React.ReactNode[]
}) {
  return (
    <details className="sync-guide" open>
      <summary>{title}</summary>
      <ol className="sync-steps">
        {steps.map((step, index) => (
          <li key={index}>
            <span className="sync-step-index">{index + 1}</span>
            <div className="sync-step-text">{step}</div>
          </li>
        ))}
      </ol>
    </details>
  )
}

/** 兼容入口：按目标打开各自独立的同步界面，不再共用一个左侧导航。 */
export function FileSyncDialog({
  open,
  onClose,
  onSynced,
  initialTarget = 'zotero',
}: Props) {
  if (!open) return null
  if (initialTarget === 'scholaread') {
    return (
      <SyncShell open={open} onClose={onClose} tone="scholaread">
        <ScholarReadSyncPane onSynced={onSynced} />
      </SyncShell>
    )
  }
  if (initialTarget === 'phone') {
    return (
      <SyncShell open={open} onClose={onClose} tone="phone">
        <PhoneSyncPane onSynced={onSynced} />
      </SyncShell>
    )
  }
  return (
    <SyncShell open={open} onClose={onClose} tone="zotero">
      <ZoteroSyncPane onSynced={onSynced} />
    </SyncShell>
  )
}

function PhoneSyncPane({ onSynced }: { onSynced: () => void }) {
  const mobile = isMobileShell()
  return (
    <div className="file-sync-pane">
      <div className="file-sync-pane-head">
        <div>
          <h3>手机离线阅读</h3>
          <p>
            电脑导出同步包后，用微信、数据线或网盘发到手机；手机在文献库点「导入同步包」。
            不需要同一 Wi-Fi、端口或服务器。
          </p>
        </div>
        <div className="file-sync-status">
          <span className="file-sync-badge is-ok">单向：电脑 → 手机</span>
        </div>
      </div>
      <SyncGuide
        title="发送到手机：4 步"
        steps={[
          <>在下方勾选论文，建议一次 5–10 篇。</>,
          <>点「导出同步包」，生成 .paperead 文件。</>,
          <>用微信、数据线或网盘把文件发到手机。</>,
          <>在手机文献库点「导入同步包」，选择文件即可离线阅读。</>,
        ]}
      />
      <div className="file-sync-phone">
        {mobile ? (
          <MobileDeviceSyncPanel bare onDone={onSynced} />
        ) : (
          <PackageExportPanel pendingFirst />
        )}
      </div>
    </div>
  )
}

// ── Zotero ──────────────────────────────────────────────

function ZoteroSyncPane({ onSynced }: { onSynced: () => void }) {
  const [direction, setDirection] = useState<ZoteroDirection>('pull')
  const [web, setWeb] = useState<ZoteroWebStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const [local, setLocal] = useState<ZoteroStatus | null>(null)
  const [items, setItems] = useState<ZoteroItem[]>([])
  const [query, setQuery] = useState('')
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [loadingItems, setLoadingItems] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [total, setTotal] = useState(0)
  const [preview, setPreview] = useState<ZoteroItem | null>(null)

  const [papers, setPapers] = useState<PaperRow[]>([])
  const [checkedPapers, setCheckedPapers] = useState<Set<string>>(new Set())

  const [editing, setEditing] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [userId, setUserId] = useState('')
  const [libraryType, setLibraryType] = useState<'user' | 'group'>('user')
  const [groupId, setGroupId] = useState('')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [report, setReport] = useState<ZoteroWebReport | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    // 拉取只需本机数据库：先拿到本机状态让列表立即开始加载，
    // Web API 配置只在推送时需要，失败也不影响拉取。
    try {
      setLocal(await zoteroStatus())
    } catch {
      setLocal(null)
    } finally {
      setLoading(false)
    }
    try {
      const status = await getZoteroWebStatus()
      setWeb(status)
      setLibraryType(status.library_type === 'group' ? 'group' : 'user')
      setUserId(status.user_id || '')
      setGroupId(status.group_id)
      setEditing(!status.configured)
      setReport(status.last_report ?? null)
    } catch {
      setWeb(null)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const loadItems = useCallback(async (q: string, start = 0) => {
    const append = start > 0
    setLoadingItems(true)
    try {
      // 后端单页上限 100；本机库更多时用「加载更多」按页追加。
      const page = await zoteroItems({ q, limit: 100, start })
      setItems((prev) => (append ? [...prev, ...page.items] : page.items))
      setTotal(page.total)
      setHasMore(page.has_more)
      setChecked((prev) => {
        const next = append ? new Set(prev) : new Set<string>()
        for (const item of page.items) {
          if (item.pdf && !item.imported) next.add(item.key)
        }
        return next
      })
    } catch {
      if (!append) {
        setItems([])
        setTotal(0)
      }
      setHasMore(false)
    } finally {
      setLoadingItems(false)
    }
  }, [])

  useEffect(() => {
    if (direction !== 'pull' || !local?.ok) return
    void loadItems(query)
  }, [direction, local?.ok, query, loadItems])

  const loadPapers = useCallback(async () => {
    try {
      const list = await listPapers()
      setPapers(list)
      setCheckedPapers(
        new Set(list.filter((item) => item.status === 'ready').map((item) => item.id)),
      )
    } catch {
      setPapers([])
    }
  }, [])

  useEffect(() => {
    if (direction === 'push') void loadPapers()
  }, [direction, loadPapers])

  const save = async () => {
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const next = await saveZoteroWebConfig({
        api_key: apiKey,
        user_id: userId,
        library_type: libraryType,
        group_id: groupId,
      })
      setWeb(next)
      setApiKey('')
      setEditing(false)
      setNotice(next.username ? `已连接 Zotero 用户 ${next.username}` : '已保存并连接成功')
    } catch (saveError) {
      setError(errorText(saveError, '保存 Zotero 配置失败'))
    } finally {
      setSaving(false)
    }
  }

  const test = async () => {
    setTesting(true)
    setError('')
    setNotice('')
    try {
      const result = await testZoteroWeb()
      setNotice(`连接正常：${result.username || result.user_id}`)
    } catch (testError) {
      setError(errorText(testError, '连接 Zotero 失败'))
    } finally {
      setTesting(false)
    }
  }

  const pullSelected = async () => {
    if (checked.size === 0) return
    setSyncing(true)
    setError('')
    setNotice('')
    try {
      const result = await importZotero([...checked])
      setNotice(
        result.imported
          ? `已拉取 ${result.imported} 篇到文献库，正在解析`
          : '所选条目都已在文献库中',
      )
      onSynced()
      await loadItems(query)
    } catch (pullError) {
      setError(errorText(pullError, '拉取 Zotero 论文失败'))
    } finally {
      setSyncing(false)
    }
  }

  const pullAll = async () => {
    setSyncing(true)
    setError('')
    setNotice('')
    try {
      const result = await syncZotero('')
      setNotice(
        result.imported
          ? `已拉取 ${result.imported} 篇到文献库，正在解析`
          : 'Zotero 里的论文都已在本机文献库',
      )
      onSynced()
      await loadItems(query)
    } catch (pullError) {
      setError(errorText(pullError, '全部拉取失败'))
    } finally {
      setSyncing(false)
    }
  }

  const push = async (all = false) => {
    if (!all && checkedPapers.size === 0) return
    setSyncing(true)
    setError('')
    setNotice('')
    try {
      const next = await pushZoteroWeb(all ? [] : [...checkedPapers])
      setReport(next)
      setWeb((prev) =>
        prev ? { ...prev, last_sync: next.finished_at, last_report: next } : prev,
      )
      setNotice(
        next.pushed || next.added || next.updated
          ? `同步完成：推送 ${next.pushed} 篇，拉取新增 ${next.added} 篇`
          : '同步完成，两边已经一致',
      )
      onSynced()
    } catch (syncError) {
      setError(errorText(syncError, '同步到 Zotero 失败'))
    } finally {
      setSyncing(false)
    }
  }

  const readyItems = items.filter((item) => item.pdf && !item.imported)
  const stats: [string, number][] = report
    ? [
        ['本地新增', report.added],
        ['本地更新', report.updated],
        ['推送到 Zotero', report.pushed],
        ['PDF 上传', report.pdf_uploaded],
        ['PDF 下载', report.pdf_downloaded],
      ]
    : []

  return (
    <div className="file-sync-pane">
      <div className="file-sync-pane-head">
        <div>
          <h3>Zotero</h3>
          <p>
            拉取：从本机 Zotero 选择论文导入文献库；推送：与 Zotero 云端双向同步，
            把本地新增的论文、分类与 PDF 推上去。
          </p>
        </div>
        <div className="file-sync-status">
          {loading && <span className="file-sync-badge">读取中</span>}
          {direction === 'pull' ? (
            local?.ok ? (
              <span className="file-sync-badge is-ok">本机 Zotero 可用</span>
            ) : (
              <span className="file-sync-badge">未检测到本机库</span>
            )
          ) : web?.configured ? (
            <span className="file-sync-badge is-ok">
              <IconCheck /> 已连接
            </span>
          ) : (
            <span className="file-sync-badge">未配置</span>
          )}
        </div>
      </div>

      <div className="file-sync-tabs" role="tablist" aria-label="Zotero 同步方向">
        <button
          type="button"
          role="tab"
          aria-selected={direction === 'pull'}
          data-active={direction === 'pull'}
          onClick={() => setDirection('pull')}
        >
          <IconDownload /> 拉取到文献库
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={direction === 'push'}
          data-active={direction === 'push'}
          onClick={() => setDirection('push')}
        >
          <IconSend /> 推送到 Zotero
        </button>
      </div>

      {direction === 'push' && (
        <details className="file-sync-config" open={editing || !web?.configured}>
          <summary>
            {web?.configured
              ? `Zotero 连接 · ${web.user_id || '已保存'}`
              : '配置 Zotero Web API（推送或双向同步时需要）'}
          </summary>
        <div className="zotero-web-form">
          <label>
            <span>API Key</span>
            <input
              type="password"
              value={apiKey}
              autoComplete="off"
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={
                web?.api_key_masked
                  ? `已保存 ${web.api_key_masked}，留空则不修改`
                  : '在 zotero.org/settings/keys 创建带写权限的 Key'
              }
            />
          </label>
          <label>
            <span>同步到</span>
            <select
              value={libraryType}
              onChange={(event) =>
                setLibraryType(event.target.value === 'group' ? 'group' : 'user')
              }
            >
              <option value="user">我的文库</option>
              <option value="group">群组文库</option>
            </select>
          </label>
          {libraryType === 'group' && (
            <label>
              <span>群组 ID</span>
              <input
                value={groupId}
                inputMode="numeric"
                onChange={(event) => setGroupId(event.target.value)}
              />
            </label>
          )}
          <div className="zotero-web-actions">
            <button className="btn primary" onClick={() => void save()} disabled={saving}>
              {saving ? '检测中…' : '保存并检测连接'}
            </button>
            <button className="btn ghost" onClick={() => void test()} disabled={testing}>
              {testing ? '检测中…' : '检测连接'}
            </button>
            {web?.configured && (
              <button className="btn ghost" onClick={() => setEditing(false)}>
                收起
              </button>
            )}
          </div>
        </div>
      </details>
      )}

      {direction === 'pull' ? (
        <>
          <SyncGuide
            title="Zotero 拉取：3 步"
            steps={[
              <>打开 Zotero 桌面版，确认论文已经同步到本机，并下载了 PDF；应用会直接读取本机 Zotero 数据库。</>,
              <>如果列表为空，再到 Zotero「设置 → 高级」勾选“允许此计算机上的其他应用程序与 Zotero 通讯”并重启 Zotero，作为兼容回退。</>,
              <>勾选论文后点「拉取选中」；想全部导入就点「全部拉取」。</>,
            ]}
          />
          <div className="file-sync-toolbar">
            <label className="file-sync-search">
              <IconSearch />
              <input
                value={query}
                placeholder="搜索本机 Zotero 论文"
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <CheckAll
              ids={readyItems.map((item) => item.key)}
              checked={checked}
              disabled={loadingItems || syncing}
              onChange={setChecked}
            />
            <button
              className="mini-btn"
              onClick={() => setChecked(new Set())}
              disabled={checked.size === 0}
            >
              清空
            </button>
            <span className="file-sync-count">{checked.size} 篇已选</span>
          </div>

          {!local?.ok ? (
            <p className="file-sync-empty">
              {local?.error || '没有检测到本机 Zotero。请打开 Zotero 桌面版后再试。'}
            </p>
          ) : loadingItems ? (
            <p className="file-sync-empty">正在读取 Zotero 论文…</p>
          ) : items.length === 0 ? (
            <p className="file-sync-empty">
              本机 Zotero 里还没有可导入的论文或 PDF。先在 Zotero 桌面版登录并完成同步、
              把 PDF 下载到本机，回到这里会自动加载。
            </p>
          ) : (
            <div className="file-sync-list">
              {items.map((item) => (
                <PaperCheckRow
                  key={item.key}
                  tone="zotero"
                  coverUrl={mediaUrl(item.thumb_url)}
                  title={item.title}
                  subtitle={`${item.authors || '作者未知'}${item.year ? ` · ${item.year}` : ''}${
                    item.publication ? ` · ${item.publication}` : ''
                  }`}
                  tag={item.imported ? '已在库' : item.pdf ? '有 PDF' : '无 PDF'}
                  checked={checked.has(item.key)}
                  disabled={!item.pdf || item.imported || syncing}
                  onPreview={item.pdf ? () => setPreview(item) : undefined}
                  onToggle={() =>
                    setChecked((prev) => {
                      const next = new Set(prev)
                      if (next.has(item.key)) next.delete(item.key)
                      else next.add(item.key)
                      return next
                    })
                  }
                />
              ))}
            </div>
          )}

          {hasMore && (
            <div className="file-sync-more">
              <button
                className="mini-btn"
                disabled={loadingItems || syncing}
                onClick={() => void loadItems(query, items.length)}
              >
                {loadingItems ? '加载中…' : `加载更多（${items.length}/${total}）`}
              </button>
            </div>
          )}

          <div className="file-sync-actions">
            <button
              className="btn primary"
              disabled={syncing || checked.size === 0}
              onClick={() => void pullSelected()}
            >
              {syncing ? '拉取中…' : `拉取选中（${checked.size} 篇）`}
            </button>
            <button className="btn" disabled={syncing || !local?.ok} onClick={() => void pullAll()}>
              全部拉取
            </button>
          </div>
        </>
      ) : (
        <>
          <SyncGuide
            title="Zotero 推送：4 步（含 API Key 获取）"
            steps={[
              <>打开 <a href="https://www.zotero.org/settings/keys/new" target="_blank" rel="noreferrer">Zotero API Key 创建页</a>并登录。</>,
              <>勾选 <b>Allow write access</b>；如果写入群组库，同时勾选对应群组。保存后复制 Key。</>,
              <>展开上方「Zotero 连接」配置，粘贴 Key，点「保存并检测连接」；用户 ID 会自动识别。</>,
              <>勾选论文点「推送选中」，或点「全部推送」上传所有未同步论文。</>,
            ]}
          />
          <div className="file-sync-toolbar">
            <span className="file-sync-count">
              本地 {papers.length} 篇 · 已选 {checkedPapers.size} 篇
            </span>
            <CheckAll
              ids={papers.filter((item) => item.status === 'ready').map((item) => item.id)}
              checked={checkedPapers}
              disabled={syncing}
              onChange={setCheckedPapers}
            />
          </div>
          <div className="file-sync-list">
            {papers.map((paper) => (
              <PaperCheckRow
                key={paper.id}
                tone="zotero"
                coverUrl={mediaUrl(paper.thumb_url)}
                title={paper.title}
                subtitle={`${paper.authors || '作者未知'} · ${paper.pages} 页`}
                tag={paper.status === 'ready' ? '已就绪' : '解析中'}
                checked={checkedPapers.has(paper.id)}
                disabled={paper.status !== 'ready' || syncing}
                onToggle={() =>
                  setCheckedPapers((prev) => {
                    const next = new Set(prev)
                    if (next.has(paper.id)) next.delete(paper.id)
                    else next.add(paper.id)
                    return next
                  })
                }
              />
            ))}
          </div>
          <p className="file-sync-note">
            勾选后点「推送选中」只上传这些论文；点「全部推送」会上传所有未同步论文。
            两种方式都会顺带拉取 Zotero 的远端更新。
          </p>
          <div className="file-sync-actions">
            <button
              className="btn primary"
              disabled={syncing || !web?.configured || checkedPapers.size === 0}
              onClick={() => void push(false)}
            >
              {syncing ? '同步中…' : `推送选中（${checkedPapers.size} 篇）`}
            </button>
            <button
              className="btn"
              disabled={syncing || !web?.configured}
              onClick={() => void push(true)}
            >
              全部推送
            </button>
            <button className="btn ghost" disabled={syncing} onClick={() => void load()}>
              <IconRefresh /> 刷新状态
            </button>
          </div>
          <p className="file-sync-note">
            {web?.last_sync
              ? `上次同步：${syncTime(web.last_sync)}`
              : '还没有同步过；首次同步会把本地未关联的论文与 PDF 上传。'}
          </p>
          {stats.length > 0 && (
            <div className="file-sync-report">
              {stats.map(([label, value]) => (
                <div key={label}>
                  <b>{value}</b>
                  <span>{label}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {error && <div className="file-sync-alert">{error}</div>}
      {notice && <div className="file-sync-alert is-ok">{notice}</div>}

      {preview && (
        <div className="zotero-preview-backdrop" onClick={() => setPreview(null)}>
          <div
            className="zotero-preview"
            role="dialog"
            aria-modal="true"
            aria-label="Zotero PDF 预览"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="zotero-preview-head">
              <div>
                <h3>{preview.title}</h3>
                <span>
                  {preview.authors || '作者未知'}
                  {preview.year ? ` · ${preview.year}` : ''}
                </span>
              </div>
              <div className="zotero-preview-actions">
                <a
                  className="icon-btn"
                  href={zoteroPdfUrl(preview.key)}
                  target="_blank"
                  rel="noreferrer"
                  title="在新窗口打开"
                >
                  <IconExpand />
                </a>
                <button
                  className="icon-btn"
                  title="关闭预览"
                  onClick={() => setPreview(null)}
                >
                  <IconClose />
                </button>
              </div>
            </header>
            <div className="zotero-preview-body">
              <Suspense fallback={<p className="file-sync-empty">正在加载 PDF…</p>}>
                <PdfViewer url={zoteroPdfUrl(preview.key)} title={preview.title} />
              </Suspense>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── 学术阅读（Scholaread） ──────────────────────────────

function ScholarReadSyncPane({ onSynced }: { onSynced: () => void }) {
  const [direction, setDirection] = useState<ScholarDirection>('pull')
  const [settings, setSettings] = useState(loadScholarReadSettings)
  const [status, setStatus] = useState<ScholarReadStatus | null>(null)
  const [items, setItems] = useState<ScholarReadPaper[]>([])
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [last, setLast] = useState<ScholarReadSyncResult | null>(null)
  const [draftDir, setDraftDir] = useState(settings.dataDir)
  const [showDir, setShowDir] = useState(false)

  const refresh = useCallback(async (dir: string) => {
    setLoading(true)
    setError('')
    let current: ScholarReadStatus | null = null
    try {
      current = await probeScholarRead(dir)
      setStatus(current)
    } catch (loadError) {
      setStatus(null)
      setItems([])
      setChecked(new Set())
      setError(errorText(loadError, '读取靠岸学术数据失败'))
      setLoading(false)
      return
    }
    if (!current.ok) {
      setItems([])
      setChecked(new Set())
      setError(
        current.error ||
          '未找到靠岸学术数据，请确认已安装并登录，或手动指定数据目录。',
      )
      setLoading(false)
      return
    }
    try {
      const page = await listScholarReadPapers(dir)
      setItems(page.items)
      setChecked(
        new Set(
          page.items
            .filter((item) => item.hasPdf && !item.imported)
            .map((item) => item.id),
        ),
      )
      if (!page.ok && page.error) setError(page.error)
    } catch (loadError) {
      setItems([])
      setChecked(new Set())
      const message = errorText(loadError, '读取靠岸学术论文失败')
      setError(
        message.includes('Not Found') || message.includes('404')
          ? '当前桌面端后端版本较旧，缺少论文列表接口。请重启或更新 Paperead 后再试。'
          : message,
      )
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh(settings.dataDir)
  }, [refresh, settings.dataDir])

  const applyDir = () => {
    const next = saveScholarReadSettings({ dataDir: draftDir.trim() })
    setSettings(next)
    void refresh(next.dataDir)
  }

  const run = async (pull: boolean, push: boolean, ids?: string[]) => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await runScholarReadSync({
        pull,
        push,
        readingIds: ids,
      })
      if (result) {
        setLast(result)
        setNotice(
          result.ok
            ? `同步完成：导入 ${result.pull.imported} 篇，更新进度 ${result.pull.progress_updated} 篇，写回 ${result.push.updated} 篇`
            : result.error || '同步未完成',
        )
        if (result.ok) onSynced()
        await refresh(settings.dataDir)
      }
    } catch (syncError) {
      setError(errorText(syncError, '学术阅读同步失败'))
    } finally {
      setBusy(false)
    }
  }

  const ready = items.filter((item) => item.hasPdf && !item.imported)

  return (
    <div className="file-sync-pane">
      <div className="file-sync-pane-head">
        <div>
          <h3>学术阅读（Scholaread）</h3>
          <p>
            拉取：选择学术阅读里的论文导入文献库，并同步阅读进度；
            推送：把 Paperead 的阅读进度写回学术阅读（需要先退出学术阅读）。
          </p>
        </div>
        <div className="file-sync-status">
          {status?.ok ? (
            <span className="file-sync-badge is-ok">
              <IconCheck /> 已连接
            </span>
          ) : (
            <span className="file-sync-badge">{loading ? '检测中' : '未检测到数据'}</span>
          )}
          {status?.running && <span className="file-sync-badge is-warn">学术阅读运行中</span>}
        </div>
      </div>

      <div className="file-sync-tabs" role="tablist" aria-label="学术阅读同步方向">
        <button
          type="button"
          role="tab"
          aria-selected={direction === 'pull'}
          data-active={direction === 'pull'}
          onClick={() => setDirection('pull')}
        >
          <IconDownload /> 拉取到文献库
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={direction === 'push'}
          data-active={direction === 'push'}
          onClick={() => setDirection('push')}
        >
          <IconSend /> 写回阅读进度
        </button>
      </div>

      <p className="file-sync-note">
        数据目录：{status?.data_dir || settings.dataDir || '自动定位'}
        <button type="button" className="link-btn" onClick={() => setShowDir((v) => !v)}>
          {showDir ? '收起' : '目录不对？手动指定'}
        </button>
      </p>
      {showDir && (
        <div className="file-sync-dir">
          <input
            className="input"
            value={draftDir}
            placeholder="留空自动定位，例如 C:\Users\你\AppData\Roaming\scholaread"
            onChange={(event) => setDraftDir(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') applyDir()
            }}
          />
          <button className="btn" onClick={applyDir}>
            重新检测
          </button>
        </div>
      )}

      <SyncGuide
        title="靠岸学术同步：3 步"
        steps={[
          <>打开靠岸学术并确认已登录；数据目录会自动定位，找不到时可在下方手动指定。</>,
          <>勾选论文后点「拉取选中」，或点「全部拉取」导入整库。</>,
          <>要写回进度时，先完全退出靠岸学术，再切到「写回阅读进度」。</>,
        ]}
      />

      {direction === 'pull' ? (
        <>
          <div className="file-sync-toolbar">
            <span className="file-sync-count">
              共 {items.length} 篇 · 可拉取 {ready.length} 篇 · 已选 {checked.size} 篇
            </span>
            <CheckAll
              ids={ready.map((item) => item.id)}
              checked={checked}
              disabled={busy || loading}
              onChange={setChecked}
            />
            <button className="mini-btn" onClick={() => setChecked(new Set())} disabled={checked.size === 0}>
              清空
            </button>
          </div>
          {loading ? (
            <p className="file-sync-empty">正在读取学术阅读论文…</p>
          ) : items.length === 0 ? (
            <p className="file-sync-empty">
              {error
                ? '暂时无法读取靠岸学术论文列表，请查看下方提示。'
                : '靠岸学术里还没有可拉取的论文。'}
            </p>
          ) : (
            <div className="file-sync-list">
              {items.map((item) => (
                <PaperCheckRow
                  key={item.id}
                  tone="scholaread"
                  coverUrl={mediaUrl(item.thumbUrl)}
                  title={item.title}
                  subtitle={`${item.authors || '作者未知'}${
                    item.readPercent ? ` · 已读 ${Math.round(item.readPercent)}%` : ''
                  }`}
                  tag={item.imported ? '已在库' : item.hasPdf ? '有 PDF' : '无 PDF'}
                  checked={checked.has(item.id)}
                  disabled={!item.hasPdf || item.imported || busy}
                  onToggle={() =>
                    setChecked((prev) => {
                      const next = new Set(prev)
                      if (next.has(item.id)) next.delete(item.id)
                      else next.add(item.id)
                      return next
                    })
                  }
                />
              ))}
            </div>
          )}
          <div className="file-sync-actions">
            <button
              className="btn primary"
              disabled={busy || checked.size === 0}
              onClick={() => void run(true, false, [...checked])}
            >
              {busy ? '拉取中…' : `拉取选中（${checked.size} 篇）`}
            </button>
            <button
              className="btn"
              disabled={busy || !status?.ok}
              onClick={() => void run(true, false)}
            >
              全部拉取
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="file-sync-empty is-tall">
            写回会把 Paperead 里已关联学术阅读的论文阅读进度同步过去。
            学术阅读正在运行时只能拉取，完全退出后才能写回；写回前会自动备份。
          </p>
          <div className="file-sync-actions">
            <button
              className="btn primary"
              disabled={busy || !status?.ok || status?.running === true}
              onClick={() => void run(false, true)}
            >
              {busy ? '写回中…' : '写回阅读进度'}
            </button>
            <button className="btn ghost" disabled={busy} onClick={() => void refresh(settings.dataDir)}>
              <IconRefresh /> 刷新状态
            </button>
          </div>
        </>
      )}

      {last && (
        <p className="file-sync-note">
          上次同步 {formatSyncTime(last.at)}：导入 {last.pull.imported} 篇，关联{' '}
          {last.pull.linked} 篇，写回 {last.push.updated} 篇。
        </p>
      )}
      {error && <div className="file-sync-alert">{error}</div>}
      {notice && <div className="file-sync-alert is-ok">{notice}</div>}
    </div>
  )
}