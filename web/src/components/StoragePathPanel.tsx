import { useCallback, useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { open as openDialog } from '@tauri-apps/plugin-dialog'
import { getStorageInfo, relocateStorage, type StorageInfo } from '../lib/library'
import { isDesktopApp } from '../lib/update'
import { LocalFolderBrowser } from '../research/LocalFolderBrowser'
import { IconFolder } from './icons'

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败'
}

/** 设置 → 常规设置：文献库默认保存路径与自动迁移。 */
export function StoragePathPanel() {
  const [info, setInfo] = useState<StorageInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [draft, setDraft] = useState('')
  const [browserOpen, setBrowserOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setInfo(await getStorageInfo())
    } catch (loadError) {
      setError(errorText(loadError))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const pick = async () => {
    setError('')
    setNotice('')
    if (!isDesktopApp()) {
      setBrowserOpen(true)
      return
    }
    try {
      const picked = await openDialog({
        directory: true,
        multiple: false,
        title: '选择文献库保存位置',
      })
      if (typeof picked === 'string' && picked) setDraft(picked)
    } catch (pickError) {
      setError(errorText(pickError))
    }
  }

  const save = async () => {
    if (!draft || busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await relocateStorage(draft)
      setInfo({
        path: result.path,
        configured: true,
        configured_path: result.path,
        fallback: false,
        writable: true,
      })
      setDraft('')
      if (isDesktopApp()) {
        setNotice(`已复制 ${result.files} 个文件，正在重启应用…`)
        window.setTimeout(() => {
          void invoke('restart_app').catch(() => undefined)
        }, 700)
      } else {
        setNotice(`迁移完成（${result.files} 个文件）。请手动重启应用以使用新位置。`)
      }
    } catch (saveError) {
      setError(errorText(saveError))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="storage-path-panel">
      <div className="storage-path-head">
        <div>
          <h3>文献库保存路径</h3>
          <p>PDF、解析结果、研究项目与模型设置都存放在这个文件夹。</p>
        </div>
      </div>

      <div className="storage-path-row">
        <div className="storage-path-value">
          <span>当前位置</span>
          {loading ? (
            <code>读取中…</code>
          ) : (
            <code title={info?.path}>{info?.path || '—'}</code>
          )}
        </div>
        <button className="btn" onClick={() => void pick()} disabled={loading || busy}>
          <IconFolder />
          更改路径
        </button>
      </div>

      {draft && (
        <div className="storage-path-row is-draft">
          <div className="storage-path-value">
            <span>新位置</span>
            <code title={draft}>{draft}</code>
          </div>
          <div className="storage-path-actions">
            <button className="btn ghost" onClick={() => setDraft('')} disabled={busy}>
              取消
            </button>
            <button className="btn primary" onClick={() => void save()} disabled={busy}>
              {busy ? '正在迁移…' : '保存并迁移'}
            </button>
          </div>
        </div>
      )}

      {info?.fallback && (
        <p className="storage-path-warn">
          自定义路径 {info.configured_path} 当前不可用，正在使用默认文献库；原路径恢复后重启应用即可继续使用。
        </p>
      )}

      {draft && !busy && (
        <p className="storage-path-hint">
          迁移只复制数据；应用重启并确认新位置可用后，旧目录会被清理。
        </p>
      )}
      {error && <p className="storage-path-error">{error}</p>}
      {notice && <p className="storage-path-notice">{notice}</p>}

      <LocalFolderBrowser
        open={browserOpen}
        title="选择文献库保存位置"
        initialPath={info?.path ?? ''}
        onClose={() => setBrowserOpen(false)}
        onSelect={(path) => {
          setBrowserOpen(false)
          setDraft(path)
        }}
      />
    </section>
  )
}