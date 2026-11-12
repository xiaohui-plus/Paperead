import { useEffect, useRef, useState } from 'react'
import {
  IconCheck,
  IconChevron,
  IconFolder,
  IconPlus,
  IconSearch,
} from '../components/icons'
import type { ProjectSummary } from '../lib/researchApi'
import { CreateProjectDialog } from './CreateProjectDialog'

interface Props {
  projects: ProjectSummary[]
  value: string
  disabled?: boolean
  onChange: (value: string) => void
  onCreate: (
    name: string,
    rootPath: string,
  ) => Promise<{ ok: boolean; project?: ProjectSummary; error?: string }>
}

/**
 * 研究消息框顶部的「选择项目」：一个入口同时承担项目切换与新建。
 *
 * 下拉里可以搜索已有项目，也可以打开创建弹窗；项目名与工作文件夹在同一个
 * 组件里确定，不再在消息框里并排出现项目名和文件夹两个标识。
 */
export function ProjectPicker({
  projects,
  value,
  disabled = false,
  onChange,
  onCreate,
}: Props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

  const current =
    value === 'auto'
      ? (projects[0] ?? null)
      : (projects.find((project) => project.id === value) ?? null)
  const label =
    value === 'new' ? '新项目' : current ? current.name : '选择项目'
  const keyword = query.trim().toLowerCase()
  const list = keyword
    ? projects.filter((project) =>
        (project.name || '').toLowerCase().includes(keyword),
      )
    : projects

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

  useEffect(() => {
    if (!open) setQuery('')
  }, [open])

  return (
    <div className="project-picker" ref={rootRef}>
      <button
        type="button"
        className="workspace-picker-button project-picker-trigger"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        title={current?.rootPath || label}
        onClick={() => setOpen((prev) => !prev)}
      >
        <IconFolder />
        <span className="workspace-picker-name">{label}</span>
        <IconChevron />
      </button>

      {open && (
        <div className="workspace-menu project-picker-menu" role="menu">
          <label className="project-picker-search">
            <IconSearch />
            <input
              autoFocus
              value={query}
              placeholder="搜索项目"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>

          {list.length === 0 ? (
            <p className="workspace-menu-empty">
              {keyword ? '没有匹配的项目' : '还没有项目'}
            </p>
          ) : (
            <ul className="workspace-menu-list">
              {list.map((project) => (
                <li key={project.id}>
                  <button
                    type="button"
                    role="menuitem"
                    className="workspace-item"
                    onClick={() => {
                      onChange(project.id)
                      setOpen(false)
                    }}
                  >
                    <IconFolder />
                    <span>
                      <strong>{project.name}</strong>
                      <small>{project.rootPath || '项目默认文件夹'}</small>
                    </span>
                    {value === project.id && <IconCheck />}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="workspace-menu-foot">
            <button
              type="button"
              className="project-picker-create"
              onClick={() => {
                setOpen(false)
                setCreateOpen(true)
              }}
            >
              <IconPlus />
              创建项目
            </button>
          </div>
        </div>
      )}

      <CreateProjectDialog
        open={createOpen}
        busy={disabled}
        onClose={() => setCreateOpen(false)}
        onCreate={async (name, rootPath) => {
          const result = await onCreate(name, rootPath)
          if (result.ok && result.project) {
            onChange(result.project.id)
            setCreateOpen(false)
          }
          return result
        }}
      />
    </div>
  )
}