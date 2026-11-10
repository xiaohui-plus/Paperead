import { useCallback, useEffect, useState } from 'react'
import {
  getZoteroWebStatus,
  runZoteroWebSync,
  saveZoteroWebConfig,
  testZoteroWeb,
  type ZoteroWebReport,
  type ZoteroWebStatus,
} from '../lib/library'
import { IconCheck, IconClose, IconRefresh } from './icons'
import { ScholarReadPanel } from './ScholarReadPanel'

type SyncSource = 'zotero' | 'scholaread'

interface Props {
  open: boolean
  onClose: () => void
  /** 同步完成后让文献库刷新列表 */
  onSynced: () => void
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请重试'
}

function syncTime(value: number): string {
  return value ? new Date(value * 1000).toLocaleString() : '尚未同步'
}

/** 文献同步：下拉框选择同步来源（Zotero / 学术阅读）。 */
export function ZoteroSyncDialog({ open, onClose, onSynced }: Props) {
  const [source, setSource] = useState<SyncSource>('zotero')
  const [status, setStatus] = useState<ZoteroWebStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [userId, setUserId] = useState('')
  const [libraryType, setLibraryType] = useState<'user' | 'group'>('user')
  const [groupId, setGroupId] = useState('')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [report, setReport] = useState<ZoteroWebReport | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const next = await getZoteroWebStatus()
      setStatus(next)
      setLibraryType(next.library_type === 'group' ? 'group' : 'user')
      setUserId(next.user_id || '')
      setGroupId(next.group_id)
      setEditing(!next.configured)
      setReport(next.last_report ?? null)
    } catch (loadError) {
      setError(errorText(loadError))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setNotice('')
    setApiKey('')
    void load()
  }, [open, load])

  if (!open) return null

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
      setStatus(next)
      setApiKey('')
      setEditing(false)
      setNotice(
        next.username ? `已连接 Zotero 用户 ${next.username}` : '已保存并连接成功',
      )
    } catch (saveError) {
      setError(errorText(saveError))
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
      setError(errorText(testError))
    } finally {
      setTesting(false)
    }
  }

  const sync = async () => {
    setSyncing(true)
    setError('')
    setNotice('')
    try {
      const next = await runZoteroWebSync()
      setReport(next)
      setStatus((prev) =>
        prev ? { ...prev, last_sync: next.finished_at, last_report: next } : prev,
      )
      setNotice(
        next.added || next.updated || next.pushed
          ? '同步完成，文献库已更新'
          : '同步完成，两边已经一致',
      )
      onSynced()
    } catch (syncError) {
      setError(errorText(syncError))
    } finally {
      setSyncing(false)
    }
  }

  const stats: [string, number][] = report
    ? [
        ['本地新增', report.added],
        ['本地更新', report.updated],
        ['推送到 Zotero', report.pushed],
        ['PDF 上传', report.pdf_uploaded],
        ['PDF 下载', report.pdf_downloaded],
        ['分类推送', report.collections_pushed],
        ['分类拉取', report.collections_pulled],
        ['归属调整', report.membership_pushed + report.membership_pulled],
        ['远端已删除', report.remote_deleted],
        ['跳过（无 PDF）', report.skipped],
      ]
    : []

  return (
    <div className="modal-mask" onMouseDown={onClose}>
      <div
        className="modal zotero-modal"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal
        aria-label="文献同步"
      >
        <header className="modal-head">
          <div>
            <h2>文献同步</h2>
            <p>
              {source === 'zotero'
                ? '通过 Zotero 官方 Web API 双向同步：本地新增的论文、分类和归属会推送到 Zotero，Zotero 里的改动也会同步回来。'
                : '从本机学术阅读（Scholaread）拉取论文与阅读进度；学术阅读退出后可把进度写回。同步需手动触发。'}
            </p>
          </div>
          <div className="sync-source">
            <select
              value={source}
              onChange={(event) =>
                setSource(event.target.value === 'scholaread' ? 'scholaread' : 'zotero')
              }
              aria-label="同步来源"
            >
              <option value="zotero">Zotero</option>
              <option value="scholaread">学术阅读（Scholaread）</option>
            </select>
          </div>
          <button className="icon-btn" onClick={onClose} title="关闭">
            <IconClose />
          </button>
        </header>

        {source === 'scholaread' ? (
          <ScholarReadPanel embedded />
        ) : (
        <div className="modal-body zotero-web">
          {loading ? (
            <div className="zotero-empty">
              <div className="spinner" />
              正在读取同步配置…
            </div>
          ) : editing ? (
            <div className="zotero-web-form">
              <label>
                <span>API Key</span>
                <input
                  type="password"
                  value={apiKey}
                  autoComplete="off"
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder={
                    status?.api_key_masked
                      ? `已保存 ${status.api_key_masked}，留空则不修改`
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
                    placeholder="群组设置页 URL 里的数字"
                  />
                </label>
              )}
              <p className="zotero-web-hint">
                还差一步：到 Zotero 官网创建一个带「写权限」的 API Key，粘贴到上方即可，
                用户 ID 会自动识别。
                <a
                  className="zotero-web-link"
                  href="https://www.zotero.org/settings/keys/new"
                  target="_blank"
                  rel="noreferrer"
                >
                  打开创建页面
                </a>
              </p>
              <div className="zotero-web-actions">
                <button
                  className="btn primary"
                  onClick={() => void save()}
                  disabled={saving}
                >
                  {saving ? '检测中…' : '保存并检测连接'}
                </button>
                {status?.configured && (
                  <button
                    className="btn ghost"
                    onClick={() => {
                      setEditing(false)
                      setError('')
                      setNotice('')
                    }}
                  >
                    取消
                  </button>
                )}
              </div>
            </div>
          ) : (
            <>
              <div className="zotero-web-status">
                <div>
                  <strong>
                    <IconCheck /> 已连接 Zotero
                    {status?.library_type === 'group'
                      ? `群组 ${status.group_id}`
                      : `用户 ${status?.user_id}`}
                  </strong>
                  <span>
                    API Key {status?.api_key_masked} · 上次同步{' '}
                    {syncTime(status?.last_sync ?? 0)}
                  </span>
                </div>
                <div className="zotero-web-actions">
                  <button
                    className="btn ghost"
                    onClick={() => void test()}
                    disabled={testing || syncing}
                  >
                    {testing ? '检测中…' : '检测连接'}
                  </button>
                  <button
                    className="btn ghost"
                    onClick={() => {
                      setEditing(true)
                      setError('')
                      setNotice('')
                    }}
                    disabled={syncing}
                  >
                    重新配置
                  </button>
                  <button
                    className="btn primary"
                    onClick={() => void sync()}
                    disabled={syncing}
                  >
                    <IconRefresh />
                    {syncing ? '同步中…' : '立即双向同步'}
                  </button>
                </div>
              </div>

              {syncing ? (
                <p className="zotero-web-hint">
                  正在同步文献、分类和 PDF。首次同步或文献较多时可能需要几分钟，请不要关闭窗口。
                </p>
              ) : (
                <p className="zotero-web-hint">
                  首次同步会把本地还没关联到 Zotero 的论文（含 PDF
                  附件）上传到 Zotero；之后只同步有变化的条目。
                </p>
              )}

              {report && !syncing && (
                <div className="zotero-web-report">
                  <h3>上次同步结果</h3>
                  <div className="zotero-web-stats">
                    {stats.map(([label, value]) => (
                      <div key={label} data-zero={value === 0}>
                        <strong>{value}</strong>
                        <span>{label}</span>
                      </div>
                    ))}
                  </div>
                  {report.remote_deleted > 0 && (
                    <p className="zotero-web-hint warn">
                      Zotero 里有 {report.remote_deleted}{' '}
                      篇文献已删除。为避免误删阅读进度和标注，本地暂不自动删除。
                    </p>
                  )}
                  {report.errors.length > 0 && (
                    <details className="zotero-web-errors">
                      <summary>{report.errors.length} 条未完成的同步</summary>
                      <ul>
                        {report.errors.map((text) => (
                          <li key={text}>{text}</li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              )}

              <p className="zotero-web-hint">
                双向同步会以 Zotero 远端元数据为准更新本地标题与作者；本地只读，
                不会反向覆盖 Zotero 的元数据。删除操作不会自动双向传播。
              </p>
            </>
          )}

          {error && <div className="zotero-result warn">{error}</div>}
          {notice && <div className="zotero-result">{notice}</div>}
        </div>
        )}
      </div>
    </div>
  )
}
