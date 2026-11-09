import { useEffect, useRef, useState } from 'react'
import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent,
  ReactNode,
} from 'react'
import {
  IconChevron,
  IconClipboard,
  IconCopy,
  IconEdit,
  IconFolder,
  IconImage,
  IconList,
  IconPdf,
  IconPlus,
  IconRefresh,
  IconScissors,
  IconSpark,
  IconTrash,
  IconUpload,
} from '../components/icons'
import type { ProjectFile } from '../lib/researchApi'

interface Props {
  items: ProjectFile[]
  activeFolderId: string | null
  activeFileId: string | null
  onFolder: (folder: ProjectFile) => void
  onFile: (file: ProjectFile) => void
  onRefresh?: () => void
  onUpload?: () => void
  onUploadFolder?: () => void
  onCreateFolder?: () => void
  onRename?: (item: ProjectFile, name: string) => void | Promise<void>
  onDelete?: (item: ProjectFile) => void
  onMove?: (item: ProjectFile, parentId: string | null) => void | Promise<void>
  onCopy?: (item: ProjectFile, parentId: string | null) => void | Promise<void>
  onCopyPath?: (item: ProjectFile) => void
  onAddToLibrary?: (item: ProjectFile) => void
  selectedFiles?: string[]
  onMultiSelect?: (itemId: string) => void
  onDeleteSelected?: () => void
}

type Clipboard = { ids: string[]; mode: 'cut' | 'copy' }

function iconFor(item: ProjectFile) {
  if (item.kind === 'folder') return <IconFolder />
  if (item.kind === 'artifact') return <IconSpark />
  if (item.mime === 'application/pdf' || item.name.toLowerCase().endsWith('.pdf')) {
    return <IconPdf />
  }
  if (item.mime.startsWith('image/')) return <IconImage />
  return <IconList />
}

function isPdf(item: ProjectFile): boolean {
  return (
    item.kind !== 'folder' &&
    (item.kind === 'paper' ||
      item.mime === 'application/pdf' ||
      item.name.toLowerCase().endsWith('.pdf'))
  )
}

/** 产物等虚拟节点是只读的：不能重命名、拖拽、剪切、删除。 */
function isReadOnly(item: ProjectFile): boolean {
  return item.meta?.readOnly === true
}

/** 资源管理器式文件树：默认只展开顶层，支持右键菜单、拖拽移动与多选。 */
export function ProjectFileTree({
  items,
  activeFolderId,
  activeFileId,
  onFolder,
  onFile,
  onRefresh,
  onUpload,
  onUploadFolder,
  onCreateFolder,
  onRename,
  onDelete,
  onMove,
  onCopy,
  onCopyPath,
  onAddToLibrary,
  selectedFiles,
  onMultiSelect,
  onDeleteSelected,
}: Props) {
  // 记录“已展开”的文件夹，空集合 = 默认全部折叠，只显示顶层。
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [menu, setMenu] = useState<{ item: ProjectFile; x: number; y: number } | null>(null)
  const [clipboard, setClipboard] = useState<Clipboard | null>(null)
  const [dragId, setDragId] = useState<string | null>(null)
  const [dropId, setDropId] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const autoExpanded = useRef<Set<string>>(new Set())

  const byId = new Map(items.map((item) => [item.id, item]))
  const activeItem = activeFileId ? byId.get(activeFileId) : undefined
  const children = new Map<string | null, ProjectFile[]>()
  for (const item of items) {
    const key = item.parentId ?? null
    const bucket = children.get(key)
    if (bucket) bucket.push(item)
    else children.set(key, [item])
  }
  for (const bucket of children.values()) {
    bucket.sort((a, b) => {
      if (a.kind === 'folder' && b.kind !== 'folder') return -1
      if (a.kind !== 'folder' && b.kind === 'folder') return 1
      return a.name.localeCompare(b.name)
    })
  }

  useEffect(() => {
    if (!renamingId) return
    const input = inputRef.current
    if (input) {
      input.focus()
      input.select()
    }
  }, [renamingId])

  // 标了 defaultOpen 的节点（如「产物」）在数据加载后自动展开一次。
  useEffect(() => {
    const pending = items.filter(
      (item) => item.meta?.defaultOpen === true && !autoExpanded.current.has(item.id),
    )
    if (pending.length === 0) return
    for (const item of pending) autoExpanded.current.add(item.id)
    setExpanded((prev) => {
      const next = new Set(prev)
      for (const item of pending) next.add(item.id)
      return next
    })
  }, [items])

  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenu(null)
    }
    document.addEventListener('click', close)
    document.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('click', close)
      document.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
      window.removeEventListener('keydown', onKey)
    }
  }, [menu])

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const isDescendantOf = (candidateId: string, ancestorId: string) => {
    let current = byId.get(candidateId)
    const seen = new Set<string>()
    while (current?.parentId) {
      if (current.parentId === ancestorId) return true
      if (seen.has(current.parentId)) return false
      seen.add(current.parentId)
      current = byId.get(current.parentId)
    }
    return false
  }

  const startRename = (item: ProjectFile) => {
    setRenamingId(item.id)
    setDraft(item.name)
  }

  const commitRename = (item: ProjectFile) => {
    if (renamingId !== item.id) return
    const value = draft.trim()
    setRenamingId(null)
    if (value && value !== item.name) onRename?.(item, value)
  }

  /** 右键/快捷键作用的对象：点中已选项时用多选，否则只处理当前项。 */
  const selectionFor = (item: ProjectFile): string[] => {
    const selected = selectedFiles ?? []
    return selected.includes(item.id) ? selected : [item.id]
  }

  /** 粘贴目标：当前文件夹，或当前文件所在的文件夹。 */
  const currentParentId = (): string | null => {
    const active = activeFileId
      ? byId.get(activeFileId)
      : activeFolderId
        ? byId.get(activeFolderId)
        : undefined
    if (!active) return null
    return active.kind === 'folder' ? active.id : active.parentId ?? null
  }

  /** 某一项允许粘贴到哪个文件夹；undefined 表示不允许。 */
  const pasteTargetFor = (item: ProjectFile): string | null | undefined => {
    if (item.kind === 'folder') {
      return isReadOnly(item) ? undefined : item.id
    }
    const parentId = item.parentId ?? null
    const parent = parentId ? byId.get(parentId) : undefined
    if (parent && isReadOnly(parent)) return undefined
    return parentId
  }

  const applyClipboard = (ids: string[], mode: 'cut' | 'copy') => {
    const usable = ids
      .map((id) => byId.get(id))
      .filter((entry): entry is ProjectFile => Boolean(entry))
      .filter((entry) => !isReadOnly(entry))
    if (usable.length === 0) return
    setClipboard({ ids: usable.map((entry) => entry.id), mode })
  }

  const copySelection = (item: ProjectFile, mode: 'cut' | 'copy') => {
    applyClipboard(selectionFor(item), mode)
  }

  const pasteTo = async (parentId: string | null) => {
    if (!clipboard) return
    const mode = clipboard.mode
    const sources = clipboard.ids
      .map((id) => byId.get(id))
      .filter((entry): entry is ProjectFile => Boolean(entry))
      // 不能把文件夹粘进它自己或它的子目录。
      .filter(
        (entry) =>
          !parentId ||
          (parentId !== entry.id && !isDescendantOf(parentId, entry.id)),
      )
    setMenu(null)
    if (mode === 'cut') setClipboard(null)
    if (sources.length === 0) return
    if (parentId) {
      setExpanded((prev) => new Set(prev).add(parentId))
    }
    for (const item of sources) {
      if (mode === 'cut') await onMove?.(item, parentId)
      else await onCopy?.(item, parentId)
    }
  }

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (renamingId) return
    const mod = event.ctrlKey || event.metaKey
    const key = event.key.toLowerCase()
    const active = activeFileId
      ? byId.get(activeFileId)
      : activeFolderId
        ? byId.get(activeFolderId)
        : undefined
    if (mod && (key === 'c' || key === 'x')) {
      event.preventDefault()
      const ids =
        selectedFiles && selectedFiles.length > 0
          ? selectedFiles
          : active
            ? [active.id]
            : []
      applyClipboard(ids, key === 'c' ? 'copy' : 'cut')
      return
    }
    if (mod && key === 'v') {
      if (!clipboard) return
      event.preventDefault()
      void pasteTo(currentParentId())
      return
    }
    if (event.key === 'F2' && active && !isReadOnly(active)) {
      event.preventDefault()
      startRename(active)
      return
    }
    if (event.key === 'Delete' && active) {
      const ids =
        selectedFiles && selectedFiles.length > 0
          ? selectedFiles
          : [active.id]
      if (ids.length > 1 && onDeleteSelected) {
        event.preventDefault()
        onDeleteSelected()
        return
      }
      const target = byId.get(ids[0]) ?? active
      if (!isReadOnly(target)) {
        event.preventDefault()
        onDelete?.(target)
      }
    }
  }

  const openMenu = (event: MouseEvent, item: ProjectFile) => {
    event.preventDefault()
    event.stopPropagation()
    setMenu({ item, x: event.clientX, y: event.clientY })
  }

  const draggingItem = dragId ? byId.get(dragId) ?? null : null
  const clipItems = clipboard
    ? clipboard.ids
        .map((id) => byId.get(id))
        .filter((entry): entry is ProjectFile => Boolean(entry))
    : []
  const clipLabel = clipItems[0]
    ? clipItems.length > 1
      ? `${clipItems[0].name} 等 ${clipItems.length} 项`
      : clipItems[0].name
    : ''
  const fileCount = items.reduce(
    (count, item) => (item.kind === 'folder' ? count : count + 1),
    0,
  )

  const renderLevel = (parentId: string | null, depth: number): ReactNode[] => {
    const rows = children.get(parentId) ?? []
    return rows.map((item) => {
      const isFolder = item.kind === 'folder'
      const readOnly = isReadOnly(item)
      const isOpen = expanded.has(item.id)
      const renaming = renamingId === item.id
      return (
        <div key={item.id}>
          <div
            className="project-tree-row"
            role="button"
            tabIndex={0}
            draggable={!renaming && !readOnly}
            data-folder={isFolder}
            data-readonly={readOnly}
            data-active={
              isFolder ? item.id === activeFolderId : item.id === activeFileId
            }
            data-multi={!isFolder && (selectedFiles ?? []).includes(item.id)}
            data-cut={clipboard?.mode === 'cut' && clipboard.ids.includes(item.id)}
            data-drag={dragId === item.id}
            data-drop={dropId === item.id}
            style={{ paddingLeft: 6 + depth * 14 }}
            title={item.path || item.name}
            onClick={(event) => {
              if (renaming) return
              if (isFolder) {
                toggle(item.id)
                onFolder(item)
              } else if ((event.ctrlKey || event.metaKey) && !readOnly) {
                onMultiSelect?.(item.id)
              } else {
                onFile(item)
              }
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !renaming) {
                if (isFolder) toggle(item.id)
                else onFile(item)
              }
            }}
            onContextMenu={(event) => openMenu(event, item)}
            onDragStart={(event) => {
              if (readOnly) return
              event.stopPropagation()
              setDragId(item.id)
              event.dataTransfer.setData('text/plain', item.id)
              event.dataTransfer.effectAllowed = 'move'
            }}
            onDragEnd={(event) => {
              event.stopPropagation()
              setDragId(null)
              setDropId(null)
            }}
            onDragOver={(event) => {
              if (!isFolder || readOnly || !dragId || dragId === item.id) return
              if (isDescendantOf(item.id, dragId)) return
              event.preventDefault()
              event.stopPropagation()
              event.dataTransfer.dropEffect = 'move'
              setDropId(item.id)
            }}
            onDragLeave={(event) => {
              event.stopPropagation()
              setDropId((prev) => (prev === item.id ? null : prev))
            }}
            onDrop={(event) => {
              event.preventDefault()
              event.stopPropagation()
              setDropId(null)
              if (!isFolder || readOnly || !draggingItem || draggingItem.id === item.id) return
              if (isDescendantOf(item.id, draggingItem.id)) return
              onMove?.(draggingItem, item.id)
              setDragId(null)
            }}
          >
            {isFolder ? (
              <i className="project-tree-chevron" data-open={isOpen}>
                <IconChevron />
              </i>
            ) : (
              <i className="project-tree-dot" />
            )}
            <i className="project-tree-icon">{iconFor(item)}</i>
            {renaming ? (
              <input
                ref={inputRef}
                className="project-tree-input"
                value={draft}
                maxLength={180}
                onClick={(event) => event.stopPropagation()}
                onChange={(event) => setDraft(event.target.value)}
                onBlur={() => commitRename(item)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    event.currentTarget.blur()
                  }
                  if (event.key === 'Escape') {
                    event.preventDefault()
                    setRenamingId(null)
                  }
                }}
              />
            ) : (
              <span className="project-tree-name">{item.name}</span>
            )}
            {!renaming && (
              <button
                type="button"
                className="project-tree-more"
                title="更多操作：重命名、复制、剪切、粘贴、删除"
                aria-label={`${item.name} 的更多操作`}
                onClick={(event) => {
                  event.stopPropagation()
                  const rect = event.currentTarget.getBoundingClientRect()
                  setMenu({ item, x: rect.right - 4, y: rect.bottom + 2 })
                }}
                onKeyDown={(event) => event.stopPropagation()}
              >
                ⋯
              </button>
            )}
          </div>
          {isFolder && isOpen && renderLevel(item.id, depth + 1)}
        </div>
      )
    })
  }

  return (
    <div
      className="project-tree"
      onKeyDown={onKeyDown}
      onDragOver={(event) => {
        if (!dragId || event.target !== event.currentTarget) return
        event.preventDefault()
        event.dataTransfer.dropEffect = 'move'
        setDropId('__root__')
      }}
      onDragLeave={(event) => {
        if (event.target !== event.currentTarget) return
        setDropId((prev) => (prev === '__root__' ? null : prev))
      }}
      onDrop={(event) => {
        if (event.target !== event.currentTarget || !draggingItem) return
        event.preventDefault()
        setDragId(null)
        setDropId(null)
        if (draggingItem.parentId) onMove?.(draggingItem, null)
      }}
    >
      <div className="project-tree-toolbar">
        {onCreateFolder && (
          <button type="button" title="新建文件夹" onClick={onCreateFolder}>
            <IconPlus />
            <span>新建</span>
          </button>
        )}
        {onUpload && (
          <button type="button" title="上传文件" onClick={onUpload}>
            <IconUpload />
            <span>上传</span>
          </button>
        )}
        {onUploadFolder && (
          <button
            type="button"
            title="打开自己电脑上的文件夹，保留子目录导入项目"
            onClick={onUploadFolder}
          >
            <IconFolder />
            <span>打开文件夹</span>
          </button>
        )}
        {onRefresh && (
          <button type="button" title="刷新文件列表" onClick={onRefresh}>
            <IconRefresh />
          </button>
        )}
        {clipboard && clipItems.length > 0 && (
          <button
            type="button"
            title={`粘贴到当前文件夹（${clipLabel}）`}
            onClick={() => void pasteTo(currentParentId())}
          >
            <IconClipboard />
            <span>粘贴</span>
          </button>
        )}
        {selectedFiles && selectedFiles.length > 0 && onDeleteSelected && (
          <button
            type="button"
            className="is-danger"
            title="删除选中的文件"
            onClick={onDeleteSelected}
          >
            <IconTrash />
            <span>删除({selectedFiles.length})</span>
          </button>
        )}
        {(!selectedFiles || selectedFiles.length === 0) &&
          activeItem &&
          onDelete &&
          !isReadOnly(activeItem) && (
            <button
              type="button"
              className="is-danger"
              title="删除当前文件"
              onClick={() => onDelete(activeItem)}
            >
              <IconTrash />
              <span>删除</span>
            </button>
          )}
        <em>
          {fileCount} 个文件
          {selectedFiles && selectedFiles.length > 0
            ? ` · 已选 ${selectedFiles.length}`
            : ''}
        </em>
      </div>

      {items.length === 0 ? (
        <p className="project-rail-empty">还没有文件</p>
      ) : (
        renderLevel(null, 0)
      )}

      {menu && (
        <div
          className="project-tree-menu"
          style={{
            left: Math.min(menu.x, Math.max(8, window.innerWidth - 184)),
            top: Math.min(menu.y, Math.max(8, window.innerHeight - 300)),
          }}
          onClick={(event) => event.stopPropagation()}
        >
          {isPdf(menu.item) && !isReadOnly(menu.item) && (
            <button
              type="button"
              onClick={() => {
                onAddToLibrary?.(menu.item)
                setMenu(null)
              }}
            >
              <IconPlus />
              加入文献库
            </button>
          )}
          {!isReadOnly(menu.item) && (
            <button
              type="button"
              onClick={() => {
                startRename(menu.item)
                setMenu(null)
              }}
            >
              <IconEdit />
              重命名
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              onCopyPath?.(menu.item)
              setMenu(null)
            }}
          >
            <IconCopy />
            复制路径
          </button>
          {!isReadOnly(menu.item) && (
            <>
              <div className="project-tree-menu-sep" />
              <button
                type="button"
                onClick={() => {
                  copySelection(menu.item, 'cut')
                  setMenu(null)
                }}
              >
                <IconScissors />
                剪切
                {selectionFor(menu.item).length > 1
                  ? ` ${selectionFor(menu.item).length} 项`
                  : ''}
              </button>
              <button
                type="button"
                onClick={() => {
                  copySelection(menu.item, 'copy')
                  setMenu(null)
                }}
              >
                <IconCopy />
                复制
                {selectionFor(menu.item).length > 1
                  ? ` ${selectionFor(menu.item).length} 项`
                  : ''}
              </button>
            </>
          )}
          {clipboard && pasteTargetFor(menu.item) !== undefined && (
            <button
              type="button"
              onClick={() => void pasteTo(pasteTargetFor(menu.item) ?? null)}
            >
              <IconClipboard />
              粘贴
              {clipboard.ids.length > 1 ? ` ${clipboard.ids.length} 项` : ''}
            </button>
          )}
          {!isReadOnly(menu.item) && (
            <>
              <div className="project-tree-menu-sep" />
              <button
                type="button"
                className="is-danger"
                onClick={() => {
                  const ids = selectionFor(menu.item)
                  if (ids.length > 1 && onDeleteSelected) onDeleteSelected()
                  else onDelete?.(menu.item)
                  setMenu(null)
                }}
              >
                <IconTrash />
                删除
                {selectionFor(menu.item).length > 1
                  ? ` ${selectionFor(menu.item).length} 项`
                  : ''}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
