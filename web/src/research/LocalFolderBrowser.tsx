import { useCallback, useEffect, useRef, useState } from 'react'
import {
  IconBack,
  IconClose,
  IconFolder,
  IconRefresh,
} from '../components/icons'
import {
  formatSize,
  researchApi,
  type FsEntry,
  type FsListing,
} from '../lib/researchApi'

interface Props {
  open: boolean
  title?: string
  initialPath?: string
  onClose: () => void
  onSelect: (path: string) => void | Promise<void>
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '无法读取这个文件夹'
}

/** Jupyter 式文件夹选择器：上面是路径，下面列出当前目录的文件夹与文件。 */
export function LocalFolderBrowser({
  open,
  title = '打开本地文件夹',
  initialPath = '',
  onClose,
  onSelect,
}: Props) {
  const [listing, setListing] = useState<FsListing | null>(null)
  const [roots, setRoots] = useState<FsEntry[]>([])
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async (path: string) => {
    setLoading(true)
    setError(null)
    try {
      const next = await researchApi.browseFs(path)
      setListing(next)
      setDraft(next.path)
    } catch (loadError) {
      setError(errorText(loadError))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setListing(null)
    setError(null)
    void load(initialPath)
    researchApi
      .fsRoots()
      .then(setRoots)
      .catch(() => setRoots([]))
  }, [open, initialPath, load])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const current = listing?.path || ''
  const canSelect = Boolean(current)

  const choose = async () => {
    if (!canSelect || busy) return
    setBusy(true)
    setError(null)
    try {
      await onSelect(current)
      onClose()
    } catch (selectError) {
      setError(errorText(selectError))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="folder-browser-backdrop" onClick={onClose}>
      <div
        className="folder-browser"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="folder-browser-head">
          <div>
            <h2>{title}</h2>
            <p>选择电脑上的文件夹作为项目工作目录</p>
          </div>
          <button className="icon-btn" title="关闭" onClick={onClose}>
            <IconClose />
          </button>
        </header>

        <div className="folder-browser-path">
          <button
            className="icon-btn"
            title="上一级"
            disabled={!listing?.parent}
            onClick={() => void load(listing?.parent || '')}
          >
            <IconBack />
          </button>
          <input
            ref={inputRef}
            value={draft}
            spellCheck={false}
            placeholder="输入文件夹路径后回车"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return
              event.preventDefault()
              void load(draft.trim())
            }}
          />
          <button
            className="icon-btn"
            title="刷新"
            onClick={() => void load(current)}
          >
            <IconRefresh />
          </button>
        </div>

        {roots.length > 0 && (
          <div className="folder-browser-roots">
            {roots.map((root) => (
              <button
                key={`${root.name}-${root.path}`}
                data-active={root.path === current}
                onClick={() => void load(root.path)}
              >
                {root.name}
              </button>
            ))}
          </div>
        )}

        <div className="folder-browser-list">
          {loading ? (
            <p className="folder-browser-note">正在读取…</p>
          ) : error ? (
            <p className="folder-browser-note is-error">{error}</p>
          ) : !listing || listing.items.length === 0 ? (
            <p className="folder-browser-note">
              {current ? '这个文件夹是空的' : '请选择一个磁盘或位置'}
            </p>
          ) : (
            listing.items.map((item) =>
              item.isDir ? (
                <button
                  key={`${item.path}`}
                  className="folder-browser-row"
                  onClick={() => void load(item.path)}
                >
                  <IconFolder />
                  <span>{item.name}</span>
                </button>
              ) : (
                <div key={`${item.path}`} className="folder-browser-row is-file">
                  <span className="folder-browser-file-dot" />
                  <span>{item.name}</span>
                  <em>{formatSize(item.size)}</em>
                </div>
              ),
            )
          )}
        </div>

        <footer className="folder-browser-foot">
          <span title={current}>{current || '未选择文件夹'}</span>
          <div>
            <button onClick={onClose}>取消</button>
            <button
              className="is-primary"
              disabled={!canSelect || busy}
              onClick={() => void choose()}
            >
              {busy ? '正在应用…' : '选择此文件夹'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}