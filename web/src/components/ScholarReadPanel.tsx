import { useCallback, useEffect, useState } from 'react'
import {
  formatSyncTime,
  isScholarReadSyncing,
  loadLastScholarReadSync,
  loadScholarReadSettings,
  probeScholarRead,
  runScholarReadSync,
  saveScholarReadSettings,
  SCHOLAREAD_SYNCED,
  type ScholarReadStatus,
  type ScholarReadSyncResult,
} from '../lib/scholareadSync'

/** 学术阅读（Scholaread）同步面板：自动定位数据目录，由用户手动触发同步。 */
export function ScholarReadPanel({ embedded = false }: { embedded?: boolean }) {
  const [settings, setSettings] = useState(loadScholarReadSettings)
  const [status, setStatus] = useState<ScholarReadStatus | null>(null)
  const [checking, setChecking] = useState(true)
  const [syncing, setSyncing] = useState(isScholarReadSyncing())
  const [last, setLast] = useState<ScholarReadSyncResult | null>(
    loadLastScholarReadSync,
  )
  const [showDir, setShowDir] = useState(false)
  const [draftDir, setDraftDir] = useState(settings.dataDir)

  const check = useCallback(async (dir: string) => {
    setChecking(true)
    try {
      setStatus(await probeScholarRead(dir))
    } catch {
      setStatus(null)
    } finally {
      setChecking(false)
    }
  }, [])

  useEffect(() => {
    void check(settings.dataDir)
  }, [check, settings.dataDir])

  useEffect(() => {
    const onSynced = () => {
      setLast(loadLastScholarReadSync())
      setSyncing(false)
      void check(settings.dataDir)
    }
    window.addEventListener(SCHOLAREAD_SYNCED, onSynced)
    return () => window.removeEventListener(SCHOLAREAD_SYNCED, onSynced)
  }, [check, settings.dataDir])

  const sync = async (pullOnly = false) => {
    setSyncing(true)
    const result = await runScholarReadSync({
      pull: true,
      push: !pullOnly,
    })
    setSyncing(false)
    setLast(result ?? loadLastScholarReadSync())
    void check(settings.dataDir)
  }

  const applyDir = () => {
    const next = saveScholarReadSettings({ dataDir: draftDir.trim() })
    setSettings(next)
    void check(next.dataDir)
  }

  const connected = Boolean(status?.ok)

  return (
    <div className={embedded ? 'scholaread-panel' : 'modal-body'}>
      <section className="model-section">
        <div className="section-head">
          <h3>学术阅读（Scholaread）</h3>
          {checking ? (
            <span className="hint">检测中…</span>
          ) : connected ? (
            <span className="scholaread-badge is-ok">已连接</span>
          ) : (
            <span className="scholaread-badge">未找到</span>
          )}
        </div>

        {connected ? (
          <>
            <p className="scholaread-path" title={status?.data_dir}>
              {status?.data_dir}
            </p>
            <div className="scholaread-stats">
              <span>
                <b>{status?.total ?? 0}</b> 篇论文
              </span>
              <span>
                <b>{status?.with_pdf ?? 0}</b> 篇可导入
              </span>
              {status?.running && <span className="is-warn">学术阅读运行中</span>}
            </div>
          </>
        ) : (
          <p className="model-empty">
            {checking
              ? '正在检测本机的学术阅读…'
              : status?.error || '未检测到学术阅读，请先安装并登录学术阅读'}
          </p>
        )}

        <button type="button" className="link-btn" onClick={() => setShowDir((v) => !v)}>
          {showDir ? '收起' : '数据目录不对？手动指定'}
        </button>
        {showDir && (
          <div className="scholaread-dir-row">
            <input
              className="input"
              value={draftDir}
              placeholder="留空自动定位，例如 C:\Users\你\AppData\Roaming\scholaread"
              onChange={(event) => setDraftDir(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') applyDir()
              }}
            />
            <button type="button" className="btn" onClick={applyDir}>
              重新检测
            </button>
          </div>
        )}
      </section>

      <section className="model-section">
        <div className="scholaread-actions">
          <button
            type="button"
            className="btn primary"
            disabled={syncing || checking || !connected}
            onClick={() => void sync(false)}
          >
            {syncing ? '同步中…' : '立即同步'}
          </button>
          <button
            type="button"
            className="btn"
            disabled={syncing || checking || !connected}
            onClick={() => void sync(true)}
          >
            仅拉取
          </button>
        </div>
        {status?.running && (
          <p className="hint">
            学术阅读正在运行，本次只会拉取；完全退出后再同步，才会写回阅读进度。
          </p>
        )}
      </section>

      {last && (
        <section className="model-section scholaread-result">
          <div className="section-head">
            <h3>上次同步</h3>
            <span className="hint">{formatSyncTime(last.at)}</span>
          </div>
          {last.error ? (
            <p className="scholaread-error">{last.error}</p>
          ) : (
            <>
              <p className="scholaread-summary">
                导入 {last.pull.imported} 篇 · 关联 {last.pull.linked} 篇 · 更新进度{' '}
                {last.pull.progress_updated} 篇 · 写回 {last.push.updated} 篇
                {last.pull.skipped_no_pdf > 0
                  ? ` · ${last.pull.skipped_no_pdf} 篇缺少本机 PDF`
                  : ''}
              </p>
              {last.push.backup && (
                <p className="scholaread-backup" title={last.push.backup}>
                  已备份：{last.push.backup.split(/[\\/]/).pop()}
                </p>
              )}
              {last.warnings.map((item) => (
                <p key={item} className="scholaread-error">
                  {item}
                </p>
              ))}
              {last.pull.errors.length > 0 && (
                <details className="scholaread-errors">
                  <summary>{last.pull.errors.length} 篇导入失败</summary>
                  <ul>
                    {last.pull.errors.map((item) => (
                      <li key={item.title + item.error}>
                        {item.title}：{item.error}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </>
          )}
        </section>
      )}
    </div>
  )
}
