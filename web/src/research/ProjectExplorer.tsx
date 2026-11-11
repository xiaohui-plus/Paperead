import { useCallback, useEffect, useState } from 'react'
import {
  IconFolder,
  IconList,
  IconPdf,
  IconRefresh,
} from '../components/icons'
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
  onChooseFolder: () => void
  onResetRoot?: () => void
  onError?: (message: string) => void
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
  if (entry.mime === 'application/pdf' || /\.pdf$/i.test(entry.name)) {
    return <IconPdf />
  }
  return <IconList />
}

/** 项目工作文件夹的 Jupyter 式浏览：上面是路径，下面是文件夹和文件。 */
export function ProjectExplorer({
  projectId,
  rootPath,
  activeFile,
  onOpenFile,
  onChooseFolder,
  onResetRoot,
  onError,
}: Props) {
  const [listing, setListing] = useState<ProjectListing | null>(null)
  const [path, setPath] = useState('')
  const [rootLabel, setRootLabel] = useState(rootPath)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

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

  const go = (target: string) => {
    if (target === path) return
    setPath(target)
  }

  const segments = path ? path.split('/').filter(Boolean) : []

  return (
    <div className="project-explorer">
      <div className="project-explorer-toolbar">
        <button
          className="project-explorer-choose"
          title="打开电脑上的任意文件夹作为项目工作目录"
          onClick={onChooseFolder}
        >
          <IconFolder />
          打开文件夹
        </button>
        <button
          className="icon-btn"
          title="刷新"
          onClick={() => setTick((value) => value + 1)}
        >
          <IconRefresh />
        </button>
        {onResetRoot && (
          <button
            className="project-explorer-reset"
            title="改用项目默认文件夹"
            onClick={onResetRoot}
          >
            默认
          </button>
        )}
      </div>

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

      <div className="project-explorer-list">
        {loading ? (
          <p className="project-rail-empty">正在读取…</p>
        ) : error ? (
          <div className="project-rail-empty">
            <p>{error}</p>
            <button
              className="project-retry"
              onClick={() => setTick((value) => value + 1)}
            >
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
            {listing?.items.map((entry) =>
              entry.isDir ? (
                <button
                  key={entry.path}
                  className="project-explorer-row"
                  title={entry.path}
                  onClick={() => go(entry.path)}
                >
                  {iconFor(entry)}
                  <span className="project-explorer-name">{entry.name}</span>
                </button>
              ) : (
                <button
                  key={entry.path}
                  className="project-explorer-row"
                  title={entry.path}
                  data-active={activeFile === entry.path}
                  onClick={() => onOpenFile(entry)}
                >
                  {iconFor(entry)}
                  <span className="project-explorer-name">{entry.name}</span>
                  <em>{formatSize(entry.size)}</em>
                </button>
              ),
            )}
          </>
        )}
      </div>
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