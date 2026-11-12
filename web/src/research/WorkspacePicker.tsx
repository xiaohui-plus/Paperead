import { useEffect, useRef, useState } from 'react'
import { open as openDialog } from '@tauri-apps/plugin-dialog'
import { IconCheck, IconChevron, IconFolder } from '../components/icons'
import { researchApi } from '../lib/researchApi'
import { isDesktopApp } from '../lib/update'
import { LocalFolderBrowser } from './LocalFolderBrowser'

/** 最近打开过的工作文件夹；会被设置备份一起同步到数据目录。 */
const RECENT_KEY = 'paperead.workspace.recent'
const RECENT_LIMIT = 8

interface Props {
  /** 本次显式选择的文件夹；空字符串表示沿用项目自己的文件夹。 */
  value: string
  /** 当前项目的 rootPath，仅用于展示。 */
  projectRoot?: string
  /** 项目还没绑定目录时，用项目名作为文件夹的默认显示名。 */
  projectName?: string
  disabled?: boolean
  onChange: (path: string) => void
}

function loadRecent(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]')
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === 'string')
      : []
  } catch {
    return []
  }
}

function remember(path: string): string[] {
  const next = [path, ...loadRecent().filter((item) => item !== path)].slice(
    0,
    RECENT_LIMIT,
  )
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    // 存不下就只在本次会话里生效
  }
  return next
}

/** Windows / macOS / Linux 路径都取最后一段展示。 */
function folderName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || path
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '这个文件夹无法打开'
}

/**
 * 开始聊天框里的工作文件夹选择器。
 *
 * 交互参考 DeepSeek Harness：点一下列出最近用过的目录，也可以调用系统
 * 文件夹选择框打开新目录；选中的目录会成为项目根目录，agent 在那里读写文件。
 */
export function WorkspacePicker({
  value,
  projectRoot = '',
  projectName = '',
  disabled = false,
  onChange,
}: Props) {
  const [open, setOpen] = useState(false)
  const [recent, setRecent] = useState<string[]>(loadRecent)
  const [browserOpen, setBrowserOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const rootRef = useRef<HTMLDivElement | null>(null)

  const effective = value || projectRoot

  useEffect(() => {
    if (!open) return
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const choose = (path: string) => {
    setError(null)
    setRecent(remember(path))
    setOpen(false)
    onChange(path)
  }

  const pickNewFolder = async () => {
    setError(null)
    if (!isDesktopApp()) {
      setOpen(false)
      setBrowserOpen(true)
      return
    }
    try {
      const picked = await openDialog({
        directory: true,
        multiple: false,
        title: '选择工作文件夹',
      })
      if (typeof picked === 'string' && picked) {
        choose(picked)
      } else {
        setOpen(false)
      }
    } catch (pickError) {
      setError(errorText(pickError))
    }
  }

  const pickRecent = async (path: string) => {
    try {
      // 目录可能已被移动或删除，先确认还能读再选。
      await researchApi.browseFs(path)
      choose(path)
    } catch (pickError) {
      setError(errorText(pickError))
      setRecent((prev) => {
        const next = prev.filter((item) => item !== path)
        try {
          localStorage.setItem(RECENT_KEY, JSON.stringify(next))
        } catch {
          // 忽略写入失败
        }
        return next
      })
    }
  }

  return (
    <div className="workspace-picker" ref={rootRef}>
      <button
        type="button"
        className="workspace-picker-button"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        title={effective || projectName || '使用项目自己的文件夹'}
        onClick={() => setOpen((prev) => !prev)}
      >
        <IconFolder />
        <span className="workspace-picker-name">
          {effective ? folderName(effective) : projectName || '项目默认文件夹'}
        </span>
        {value && <em>本次</em>}
        <IconChevron />
      </button>

      {open && (
        <div className="workspace-menu" role="menu">
          <p className="workspace-menu-title">工作文件夹</p>
          {recent.length === 0 ? (
            <p className="workspace-menu-empty">还没有打开过文件夹</p>
          ) : (
            <ul className="workspace-menu-list">
              {recent.map((path) => (
                <li key={path}>
                  <button
                    type="button"
                    role="menuitem"
                    className="workspace-item"
                    onClick={() => void pickRecent(path)}
                  >
                    <IconFolder />
                    <span>
                      <strong>{folderName(path)}</strong>
                      <small>{path}</small>
                    </span>
                    {path === effective && <IconCheck />}
                  </button>
                </li>
              ))}
            </ul>
          )}

          {error && <p className="workspace-menu-error">{error}</p>}

          <div className="workspace-menu-foot">
            <button type="button" onClick={() => void pickNewFolder()}>
              打开新文件夹…
            </button>
            {value && (
              <button
                type="button"
                className="workspace-menu-reset"
                onClick={() => {
                  setError(null)
                  setOpen(false)
                  onChange('')
                }}
              >
                改用项目默认文件夹
              </button>
            )}
          </div>
        </div>
      )}

      <LocalFolderBrowser
        open={browserOpen}
        title="选择工作文件夹"
        initialPath={effective}
        onClose={() => setBrowserOpen(false)}
        onSelect={(path) => {
          setBrowserOpen(false)
          choose(path)
        }}
      />
    </div>
  )
}
