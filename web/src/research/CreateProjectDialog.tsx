import { useEffect, useState } from 'react'
import { open as openDialog } from '@tauri-apps/plugin-dialog'
import { IconClose, IconFolder } from '../components/icons'
import { isDesktopApp } from '../lib/update'
import { LocalFolderBrowser } from './LocalFolderBrowser'

interface Props {
  open: boolean
  busy?: boolean
  onClose: () => void
  /** 返回 ok=false 时把后端原因留在弹窗里，不跳走也不关窗。 */
  onCreate: (name: string, rootPath: string) => Promise<{ ok: boolean; error?: string }>
}

function folderName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || path
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '这个文件夹无法打开'
}

/**
 * 创建本地项目：名称 + 项目文件夹。
 *
 * 文件夹按钮调用系统选择器（桌面端 dialog 插件），把选中的目录作为项目根目录，
 * agent 后续都在这个目录里读写；浏览器预览没有系统选择器时回退到目录浏览器。
 */
export function CreateProjectDialog({ open, busy = false, onClose, onCreate }: Props) {
  const [name, setName] = useState('')
  const [folder, setFolder] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [browserOpen, setBrowserOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) return
    setName('')
    setFolder('')
    setError(null)
    setBrowserOpen(false)
    setSubmitting(false)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy && !submitting) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, busy, submitting, onClose])

  if (!open) return null

  const pickFolder = async () => {
    setError(null)
    if (!isDesktopApp()) {
      setBrowserOpen(true)
      return
    }
    try {
      const picked = await openDialog({
        directory: true,
        multiple: false,
        title: '选择项目文件夹',
      })
      if (typeof picked === 'string' && picked) setFolder(picked)
    } catch (pickError) {
      setError(errorText(pickError))
    }
  }

  // 名称可以留空：为空时用所选文件夹的名字作为项目名
  const ready = Boolean(folder)
  const pending = busy || submitting

  const submit = async () => {
    if (!ready || pending) return
    setSubmitting(true)
    setError(null)
    try {
      const result = await onCreate(name.trim() || folderName(folder), folder)
      if (!result.ok) setError(result.error || '创建项目失败，请重试')
    } catch (createError) {
      setError(errorText(createError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="create-project-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !pending) onClose()
    }}>
      <section
        className="create-project-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="创建本地项目"
      >
        <header className="create-project-head">
          <h2>创建本地项目</h2>
          <button
            className="create-project-close"
            title="关闭"
            onClick={onClose}
            disabled={pending}
          >
            <IconClose />
          </button>
        </header>

        <div className="create-project-body">
          <label className="create-project-field">
            <span>名称</span>
            <div className="create-project-input">
              <IconFolder />
              <input
                autoFocus
                value={name}
                maxLength={60}
                placeholder="输入项目名称（留空则使用文件夹名）"
                disabled={pending}
                onChange={(event) => setName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') void submit()
                }}
              />
            </div>
          </label>

          <div className="create-project-field">
            <span>文件夹</span>
            <div className="create-project-folder" data-set={Boolean(folder)}>
              <i className="create-project-folder-icon">
                <IconFolder />
              </i>
              {folder ? (
                <>
                  <b>{folderName(folder)}</b>
                  <span className="create-project-path" title={folder}>
                    {folder}
                  </span>
                  <button className="btn ghost" onClick={() => void pickFolder()} disabled={pending}>
                    更换
                  </button>
                </>
              ) : (
                <>
                  <b>添加本地文件夹</b>
                  <span>将文件夹内容用作项目上下文</span>
                  <button className="btn ghost" onClick={() => void pickFolder()} disabled={pending}>
                    去添加
                  </button>
                </>
              )}
            </div>
          </div>

          {error && <p className="create-project-error">{error}</p>}
        </div>

        <footer className="create-project-foot">
          <button className="btn ghost" onClick={onClose} disabled={pending}>
            取消
          </button>
          <button className="btn primary" onClick={() => void submit()} disabled={!ready || pending}>
            {pending ? '创建中…' : '创建项目'}
          </button>
        </footer>
      </section>

      <LocalFolderBrowser
        open={browserOpen}
        title="选择项目文件夹"
        initialPath={folder}
        onClose={() => setBrowserOpen(false)}
        onSelect={(path) => {
          setBrowserOpen(false)
          setFolder(path)
        }}
      />
    </div>
  )
}