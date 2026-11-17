import { useCallback, useEffect, useRef, useState } from 'react'
import {
  IconBook,
  IconClipboard,
  IconCopy,
  IconEdit,
  IconFolder,
  IconRefresh,
  IconScissors,
  IconTrash,
} from '../components/icons'
import { FileTypeIcon } from '../components/fileTypeIcons'
import {
  formatSize,
  researchApi,
  type FsEntry,
  type ProjectListing,
} from '../lib/researchApi'

interface Props {
  projectId: string
  rootPath: string
  activeFile?: string | null
  onOpenFile: (entry: FsEntry) => void


  onError?: (message: string) => void
}

type Clipboard = {
  path: string
  name: string
  isDir: boolean
  mode: 'copy' | 'cut'
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '无法读取工作文件夹'
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || path
}

function iconFor(entry: FsEntry) {
  if (entry.isDir) return <IconFolder />
  return <FileTypeIcon name={entry.name} mime={entry.mime} />
}


/** 项目工作文件夹的 Jupyter 式浏览：双击文件预览，右键可改名、复制、剪切、删除。 */
export function ProjectExplorer({
  projectId,
  rootPath,
  activeFile,
  onOpenFile,


  onError,
}: Props) {
  const [listing, setListing] = useState<ProjectListing | null>(null)
  const [path, setPath] = useState('')
  const [rootLabel, setRootLabel] = useState(rootPath)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState('')
  const [tick, setTick] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const [menu, setMenu] = useState<{ entry: FsEntry; x: number; y: number } | null>(
    null,
  )
  const [clipboard, setClipboard] = useState<Clipboard | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const load = useCallback(
    async (target: string) => {
      setLoading(true)
      setError(null)
      try {
        const next = await researchApi.projectExplorer(projectId, target)
        setListing(next)
        setPath(next.path)
        if (next.root) setRootLabel(next.root)
      } catch (loadError) {
        const message = errorText(loadError)
        setError(message)
        onError?.(message)
      } finally {
        setLoading(false)
      }
    },
    [projectId, onError],
  )

  useEffect(() => {
    setPath('')
  }, [projectId, rootPath])

  useEffect(() => {
    void load(path)
  }, [load, path, rootPath, tick])

  useEffect(() => {
    if (!menu) return
    const close = (event: MouseEvent) => {
      if (!(event.target as Element).closest('.project-tree-menu')) setMenu(null)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenu(null)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', onKey)

    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', onKey)
    }
  }, [menu])

  useEffect(() => {
    if (!renaming) return
    const input = inputRef.current
    input?.focus()
    input?.select()
  }, [renaming])

  const go = (target: string) => {
    if (target === path) return
    setSelected(null)
    setPath(target)
  }

  const refresh = () => setTick((value) => value + 1)

  const run = async (work: () => Promise<unknown>) => {
    if (busy) return
    setBusy(true)
    setActionError('')
    try {
      await work()
      refresh()
    } catch (workError) {
      setActionError(errorText(workError))
    } finally {
      setBusy(false)
    }
  }

  const openEntry = (entry: FsEntry) => {
    if (entry.isDir) go(entry.path)
    else {
      setSelected(entry.path)
      onOpenFile(entry)
    }
  }

  const copyText = async (value: string) => {
    try {
      await navigator.clipboard?.writeText(value)
    } catch {
      setActionError('复制失败，请检查剪贴板权限。')
    }
  }

  const cut = (entry: FsEntry) => {
    setClipboard({
      path: entry.path,
      name: entry.name,
      isDir: entry.isDir,
      mode: 'cut',
    })
    setSelected(entry.path)
    setMenu(null)
  }

  const copy = (entry: FsEntry) => {
    setClipboard({
      path: entry.path,
      name: entry.name,
      isDir: entry.isDir,
      mode: 'copy',
    })
    setSelected(entry.path)
    setMenu(null)
  }

  const paste = (targetDir: string) => {
    if (!clipboard) return
    setMenu(null)
    void run(async () => {
      await researchApi.transferExplorerItem(
        projectId,
        clipboard.path,
        targetDir,
        clipboard.mode === 'cut' ? 'move' : 'copy',
      )
      if (clipboard.mode === 'cut') setClipboard(null)
    })
  }

  const remove = (entry: FsEntry) => {
    setMenu(null)
    const what = entry.isDir ? '文件夹及其中的所有文件' : '文件'
    if (!window.confirm(`确定删除${what}「${entry.name}」？删除后无法恢复。`)) {
      return
    }
    void run(async () => {
      await researchApi.deleteExplorerItem(projectId, entry.path)
      if (selected === entry.path) setSelected(null)
      if (clipboard?.path === entry.path) setClipboard(null)
    })
  }

  const startRename = (entry: FsEntry) => {
    setMenu(null)
    setRenaming(entry.path)
    setDraft(entry.name)
  }

  const commitRename = (entry: FsEntry) => {
    const name = draft.trim()
    setRenaming(null)
    if (!name || name === entry.name) return
    setSelected(null)
    void run(async () => {
      await researchApi.renameExplorerItem(projectId, entry.path, name)
    })
  }

  const segments = path ? path.split('/').filter(Boolean) : []

  return (
    <div className="project-explorer">
      <div className="project-explorer-pathline">
        <nav className="project-explorer-path" aria-label="当前路径">
          <button data-active={!path} onClick={() => go('')} title={rootLabel}>
            {baseName(rootLabel) || '工作文件夹'}
          </button>
          {segments.map((segment, index) => (
            <span key={`${segment}-${index}`}>
              <em>/</em>
              <button
                data-active={index === segments.length - 1}
                onClick={() => go(segments.slice(0, index + 1).join('/'))}
              >
                {segment}
              </button>
            </span>
          ))}
        </nav>
        {clipboard && (
          <button
            className="icon-btn project-explorer-refresh"
            title={`粘贴${clipboard.mode === 'cut' ? '（移动）' : ''}「${clipboard.name}」到当前文件夹`}
            disabled={busy}
            onClick={() => paste(path)}
          >
            <IconClipboard />
          </button>
        )}
        {selected && (
          <button
            className="icon-btn project-explorer-refresh"
            title="删除所选"
            disabled={busy}
            onClick={() => {
              const entry = listing?.items.find((item) => item.path === selected)
              if (entry) remove(entry)
            }}
          >
            <IconTrash />
          </button>
        )}
        <button
          className="icon-btn project-explorer-refresh"
          title="刷新"
          onClick={refresh}
        >
          <IconRefresh />
        </button>
      </div>

      {actionError && (
        <p className="project-explorer-error">
          {actionError}
          <button onClick={() => setActionError('')}>知道了</button>
        </p>
      )}

      <div className="project-explorer-list" onScroll={() => setMenu(null)}>
        {loading ? (
          <p className="project-rail-empty">正在读取…</p>
        ) : error ? (
          <div className="project-rail-empty">
            <p>{error}</p>
            <button className="project-retry" onClick={refresh}>
              重试
            </button>
          </div>
        ) : (
          <>
            {listing?.parent !== undefined && path && (
              <button
                className="project-explorer-row"
                onClick={() => go(listing?.parent ?? '')}
              >
                <IconBackArrow />
                <span className="project-explorer-name">上一级</span>
              </button>
            )}
            {listing && listing.items.length === 0 && (
              <p className="project-rail-empty">这个文件夹是空的</p>
            )}
            {listing?.items.map((entry) => (
              <div
                key={entry.path}
                className="project-explorer-row"
                role="button"
                tabIndex={0}
                title={entry.path}
                data-active={activeFile === entry.path || selected === entry.path}
                onClick={() => {
                  if (entry.isDir) go(entry.path)
                  else setSelected(entry.path)
                }}
                onDoubleClick={() => openEntry(entry)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') openEntry(entry)
                }}
                onContextMenu={(event) => {
                  event.preventDefault()
                  if (!entry.isDir) setSelected(entry.path)
                  setMenu({ entry, x: event.clientX, y: event.clientY })
                }}
              >
                {iconFor(entry)}
                {renaming === entry.path ? (
                  <input
                    ref={inputRef}
                    className="project-tree-input"
                    value={draft}
                    maxLength={180}
                    onClick={(event) => event.stopPropagation()}
                    onChange={(event) => setDraft(event.target.value)}
                    onBlur={() => commitRename(entry)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.preventDefault()
                        event.currentTarget.blur()
                      }
                      if (event.key === 'Escape') {
                        event.preventDefault()
                        setRenaming(null)
                      }
                    }}
                  />
                ) : (
                  <span className="project-explorer-name">{entry.name}</span>
                )}
                {!entry.isDir && renaming !== entry.path && (
                  <em>{formatSize(entry.size)}</em>
                )}
                <button
                  type="button"
                  className="project-tree-more"
                  title="更多操作"
                  aria-label={`${entry.name} 的更多操作`}
                  onClick={(event) => {
                    event.stopPropagation()
                    const rect = event.currentTarget.getBoundingClientRect()
                    setMenu({ entry, x: rect.right - 4, y: rect.bottom + 2 })
                  }}
                >
                  ⋯
                </button>
              </div>
            ))}
          </>
        )}
      </div>

      {menu && (
        <div
          className="project-tree-menu"
          style={{
            left: Math.min(menu.x, Math.max(8, window.innerWidth - 184)),
            top: Math.min(menu.y, Math.max(8, window.innerHeight - 380)),
          }}
          onClick={(event) => event.stopPropagation()}
        >
          {!menu.entry.isDir && (
            <button
              type="button"
              onClick={() => {
                onOpenFile(menu.entry)
                setMenu(null)
              }}
            >
              <IconBook />
              打开预览
            </button>
          )}
          <button type="button" onClick={() => startRename(menu.entry)}>
            <IconEdit />
            重命名
          </button>
          <div className="project-tree-menu-sep" />
          <button
            type="button"
            onClick={() => {
              void copyText(menu.entry.name)
              setMenu(null)
            }}
          >
            <IconCopy />
            复制文件名称
          </button>
          <button
            type="button"
            onClick={() => {
              void copyText(menu.entry.absolute || menu.entry.path)
              setMenu(null)
            }}
          >
            <IconCopy />
            复制文件路径
          </button>
          <div className="project-tree-menu-sep" />
          <button type="button" onClick={() => cut(menu.entry)}>
            <IconScissors />
            剪切
          </button>
          <button type="button" onClick={() => copy(menu.entry)}>
            <IconCopy />
            复制
          </button>
          {clipboard && menu.entry.isDir && (
            <button type="button" onClick={() => paste(menu.entry.path)}>
              <IconClipboard />
              粘贴到此处
            </button>
          )}
          <div className="project-tree-menu-sep" />
          <button
            type="button"
            className="is-danger"
            onClick={() => remove(menu.entry)}
          >
            <IconTrash />
            删除
          </button>
        </div>
      )}
    </div>
  )
}

function IconBackArrow() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
      <path
        d="M15 5 8 12l7 7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}