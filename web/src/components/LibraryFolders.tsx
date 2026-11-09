import { useEffect, useRef, useState, type DragEvent } from 'react'
import type { Folder } from '../lib/library'
import { IconBook, IconFolder, IconFolderPapers, IconPlus } from './icons'

interface Props {
  folders: Folder[]
  totalCount: number
  activeFolderId: string | null
  loading: boolean
  error: string | null
  dragPaperId: string | null
  dragOverFolderId: string | null
  onSelect: (folderId: string | null) => void
  onCreate: (name: string) => Promise<boolean>
  onRename: (folderId: string, name: string) => Promise<boolean>
  onDelete: (folder: Folder) => void
  onDragEnterFolder: (folderId: string) => void
  onDragLeaveFolder: (folderId: string) => void
  onDropFolder: (folderId: string) => void
}

export function LibraryFolders({
  folders,
  totalCount,
  activeFolderId,
  loading,
  error,
  dragPaperId,
  dragOverFolderId,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  onDragEnterFolder,
  onDragLeaveFolder,
  onDropFolder,
}: Props) {
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState('')
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [menuId, setMenuId] = useState<string | null>(null)
  const [menuPos, setMenuPos] = useState<{ x: number; y: number } | null>(null)

  const createRef = useRef<HTMLInputElement>(null)
  const renameRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    createRef.current?.focus()
  }, [creating])

  useEffect(() => {
    renameRef.current?.focus()
    renameRef.current?.select()
  }, [renamingId])

  useEffect(() => {
    if (!menuId) return
    const onDown = (event: MouseEvent) => {
      if (menuRef.current?.contains(event.target as Node)) return
      setMenuId(null)
      setMenuPos(null)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setMenuId(null)
        setMenuPos(null)
      }
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [menuId])

  const beginCreate = () => {
    setMenuId(null)
    setMenuPos(null)
    setCreating(true)
    setDraft('')
  }

  const submitCreate = async () => {
    const name = draft.trim()
    if (!name) {
      setCreating(false)
      return
    }
    const ok = await onCreate(name)
    if (ok) {
      setCreating(false)
      setDraft('')
    }
  }

  const beginRename = (folder: Folder) => {
    setMenuId(null)
    setMenuPos(null)
    setRenamingId(folder.id)
    setRenameDraft(folder.name)
  }

  const submitRename = async (folder: Folder) => {
    const name = renameDraft.trim()
    if (!name || name === folder.name) {
      setRenamingId(null)
      return
    }
    await onRename(folder.id, name)
    setRenamingId(null)
  }

  const onRowDragOver = (event: DragEvent<HTMLDivElement>, folder: Folder) => {
    if (!dragPaperId) return
    event.preventDefault()
    event.stopPropagation()
    event.dataTransfer.dropEffect = 'move'
    if (dragOverFolderId !== folder.id) onDragEnterFolder(folder.id)
  }

  const onRowDragLeave = (event: DragEvent<HTMLDivElement>, folder: Folder) => {
    if (!dragPaperId) return
    if (event.currentTarget.contains(event.relatedTarget as Node)) return
    onDragLeaveFolder(folder.id)
  }

  const onRowDrop = (event: DragEvent<HTMLDivElement>, folder: Folder) => {
    if (!dragPaperId) return
    event.preventDefault()
    event.stopPropagation()
    onDropFolder(folder.id)
  }

  const onListDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!dragPaperId || !listRef.current) return
    const rect = listRef.current.getBoundingClientRect()
    if (event.clientY < rect.top + 34) listRef.current.scrollTop -= 9
    else if (event.clientY > rect.bottom - 34) listRef.current.scrollTop += 9
  }

  return (
    <div className="library-folders">
      <div className="folders-head">
        <span>文件夹</span>
        <button
          className="icon-btn"
          title="新建文件夹"
          onClick={beginCreate}
          disabled={creating}
        >
          <IconPlus />
        </button>
      </div>

      {creating && (
        <form
          className="folder-create"
          onSubmit={(event) => {
            event.preventDefault()
            void submitCreate()
          }}
        >
          <input
            ref={createRef}
            value={draft}
            maxLength={40}
            placeholder="文件夹名称"
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => void submitCreate()}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setCreating(false)
                setDraft('')
              }
            }}
          />
        </form>
      )}

      {loading ? (
        <p className="folders-empty">正在加载…</p>
      ) : error ? (
        <p className="folders-empty">{error}</p>
      ) : (
        <>
          <div className="folder-list" ref={listRef} onDragOver={onListDragOver}>
            <div
              className="folder-row folder-all"
              role="button"
              tabIndex={0}
              data-active={!activeFolderId}
              onClick={() => onSelect(null)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') onSelect(null)
              }}
            >
              <i className="folder-icon">
                <IconBook />
              </i>
              <span className="folder-name">全部文献</span>
              <em className="folder-count">{totalCount}</em>
            </div>

            {folders.map((folder) => (
              <div className="folder-row-wrap" key={folder.id}>
                <div
                  className="folder-row"
                  role="button"
                  tabIndex={0}
                  data-active={folder.id === activeFolderId}
                  data-empty={folder.paper_count === 0}
                  data-drop={folder.id === dragOverFolderId}
                  onClick={() => {
                    if (renamingId === folder.id) return
                    onSelect(folder.id)
                  }}
                  onDoubleClick={() => beginRename(folder)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && renamingId !== folder.id) {
                      onSelect(folder.id)
                    }
                  }}
                  onDragOver={(event) => onRowDragOver(event, folder)}
                  onDragLeave={(event) => onRowDragLeave(event, folder)}
                  onDrop={(event) => onRowDrop(event, folder)}
                >
                  <i className="folder-icon">
                    {folder.paper_count > 0 ? <IconFolderPapers /> : <IconFolder />}
                  </i>

                  {renamingId === folder.id ? (
                    <input
                      ref={renameRef}
                      className="folder-rename"
                      value={renameDraft}
                      maxLength={40}
                      onClick={(event) => event.stopPropagation()}
                      onChange={(event) => setRenameDraft(event.target.value)}
                      onBlur={() => void submitRename(folder)}
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
                    <span className="folder-name">{folder.name}</span>
                  )}

                  {renamingId !== folder.id && folder.paper_count > 0 && (
                    <em className="folder-count">{folder.paper_count}</em>
                  )}

                  {renamingId !== folder.id && dragPaperId && (
                    <span className="folder-drop-label">松开移入</span>
                  )}
                </div>

                {renamingId !== folder.id && (
                  <button
                    className="folder-more"
                    title="文件夹操作"
                    onClick={(event) => {
                      event.stopPropagation()
                      if (menuId === folder.id) {
                        setMenuId(null)
                        setMenuPos(null)
                        return
                      }
                      const rect = event.currentTarget.getBoundingClientRect()
                      const zoom =
                        Number.parseFloat(
                          getComputedStyle(document.documentElement).zoom || '1',
                        ) || 1
                      const width = 132 * zoom
                      setMenuPos({
                        x:
                          Math.max(
                            8,
                            Math.min(
                              rect.right - width,
                              window.innerWidth - width - 8,
                            ),
                          ) / zoom,
                        y:
                          Math.min(
                            rect.bottom + 4,
                            window.innerHeight - 96 * zoom,
                          ) / zoom,
                      })
                      setMenuId(folder.id)
                    }}
                  >
                    ···
                  </button>
                )}

                {menuId === folder.id && menuPos && (
                  <div
                    className="folder-menu"
                    ref={menuRef}
                    role="menu"
                    style={{ left: menuPos.x, top: menuPos.y }}
                  >
                    <button role="menuitem" onClick={() => beginRename(folder)}>
                      重命名
                    </button>
                    <button
                      role="menuitem"
                      className="is-danger"
                      onClick={() => {
                        setMenuId(null)
                        setMenuPos(null)
                        onDelete(folder)
                      }}
                    >
                      删除文件夹
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>

          {folders.length === 0 && !creating && (
            <div className="folders-empty">
              <p>还没有文件夹</p>
              <button className="folder-empty-create" onClick={beginCreate}>
                <IconPlus />
                新建文件夹
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
