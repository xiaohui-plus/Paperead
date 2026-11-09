import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  DEMO_PAPER,
  createFolder,
  deleteFolder,
  deletePaper,
  listFolders,
  listPapers,
  movePaperToFolder,
  renameFolder,
  reparsePaper,
  uploadPapers,
  type Folder,
  type PaperRow,
} from '../lib/library'
import { PaperCard } from './PaperCard'
import { Sidebar } from './Sidebar'
import type { NavKey } from './Sidebar'
import { SettingsDialog } from './SettingsDialog'
import { ZoteroImportDialog } from './ZoteroImportDialog'
import { IconBack, IconFolder, IconGrid, IconRows, IconSettings, IconUpload } from './icons'
import { LibraryFolders } from './LibraryFolders'

type Filter = 'all' | 'ready' | 'untranslated' | 'failed'
type Sort = 'recent' | 'title' | 'pages'

interface Props {
  onOpenPaper: (id: string) => void
  onNav: (key: NavKey) => void
}

export function LibraryPage({ onOpenPaper, onNav }: Props) {
  const [papers, setPapers] = useState<PaperRow[]>([])
  const [loading, setLoading] = useState(true)
  const [demoMode, setDemoMode] = useState(false)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [sort, setSort] = useState<Sort>('recent')
  const [view, setView] = useState<'grid' | 'list'>('grid')
  const [dragOver, setDragOver] = useState(false)
  const [toast, setToast] = useState<{ text: string; kind: 'ok' | 'err' } | null>(null)
  // 设置只由用户点齿轮/侧栏打开；刷新页面不自动弹
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [zoteroOpen, setZoteroOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [folders, setFolders] = useState<Folder[]>([])
  const [foldersLoading, setFoldersLoading] = useState(true)
  const [folderError, setFolderError] = useState<string | null>(null)
  const [activeFolderId, setActiveFolderId] = useState<string | null>(null)
  const [gridAnimating, setGridAnimating] = useState(false)
  const [dragPaper, setDragPaper] = useState<PaperRow | null>(null)
  const [dragOverFolderId, setDragOverFolderId] = useState<string | null>(null)
  const [dwellActive, setDwellActive] = useState(false)
  const [dragOutDone, setDragOutDone] = useState(false)
  const [dragFolderName, setDragFolderName] = useState('')
  const dwellTimer = useRef<number | undefined>(undefined)
  const movingOut = useRef(false)
  const postDragFolder = useRef<string | null | undefined>(undefined)
  const suppressClick = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const errorText = (error: unknown) =>
    error instanceof Error ? error.message : '操作失败，请重试'

  const notify = useCallback((text: string, kind: 'ok' | 'err' = 'ok') => {
    setToast({ text, kind })
    window.setTimeout(() => setToast(null), 4200)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const rows = await listPapers()
      setPapers(rows)
      setDemoMode(false)
      try {
        setFolders(await listFolders())
        setFolderError(null)
      } catch (error) {
        setFolderError(errorText(error))
      }
    } catch {
      // 后端没起：退回内置示例，页面依然可看
      setPapers([DEMO_PAPER])
      setDemoMode(true)
      setFolders([])
      setFolderError(null)
    } finally {
      setLoading(false)
      setFoldersLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 有解析/翻译中的任务时轮询
  useEffect(() => {
    if (demoMode) return
    const active = papers.some((p) => p.status === 'parsing')
    if (!active) return
    const timer = window.setInterval(() => void refresh(), 1800)
    return () => window.clearInterval(timer)
  }, [papers, demoMode, refresh])

  const doUpload = async (files: File[]) => {
    const pdfs = files.filter((f) => f.name.toLowerCase().endsWith('.pdf'))
    if (!pdfs.length) {
      notify('只支持 PDF 文件', 'err')
      return
    }
    setUploading(true)
    try {
      const res = await uploadPapers(pdfs, activeFolderId)
      await refresh()
      if (res.added.length > 0) {
        notify(`已添加 ${res.added.length} 篇，正在解析…`)
        // 上传后直接进入阅读页；解析没完成时阅读页会自动重试
        onOpenPaper(res.added[0].id)
      } else {
        notify(`上传失败：${res.failed[0]?.error ?? '未知错误'}`, 'err')
      }
    } catch (e) {
      notify(e instanceof Error ? e.message : '上传失败', 'err')
    } finally {
      setUploading(false)
    }
  }

  const onDelete = async (paper: PaperRow) => {
    if (!window.confirm(`删除《${paper.title}》？`)) return
    try {
      await deletePaper(paper.id)
      await refresh()
      notify('已删除')
    } catch (e) {
      notify(e instanceof Error ? e.message : '删除失败', 'err')
    }
  }

  const onReparse = async (paper: PaperRow) => {
    try {
      await reparsePaper(paper.id)
      notify('已重新开始解析')
      await refresh()
    } catch (e) {
      notify(e instanceof Error ? e.message : '解析失败', 'err')
    }
  }

  const folderCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const paper of papers) {
      if (paper.folder_id) {
        counts.set(paper.folder_id, (counts.get(paper.folder_id) ?? 0) + 1)
      }
    }
    return counts
  }, [papers])

  const folderRows = useMemo(
    () =>
      folders.map((folder) => ({
        ...folder,
        paper_count: folderCounts.get(folder.id) ?? 0,
      })),
    [folders, folderCounts],
  )

  const selectedFolder =
    folders.find((folder) => folder.id === activeFolderId) ?? null

  const folderPapers = useMemo(
    () =>
      activeFolderId
        ? papers.filter((paper) => paper.folder_id === activeFolderId)
        : papers,
    [papers, activeFolderId],
  )

  useEffect(() => {
    if (activeFolderId && !folders.some((folder) => folder.id === activeFolderId)) {
      setActiveFolderId(null)
    }
  }, [folders, activeFolderId])

  useEffect(() => {
    setGridAnimating(true)
    const timer = window.setTimeout(() => setGridAnimating(false), 180)
    return () => window.clearTimeout(timer)
  }, [activeFolderId])

  const handleCreateFolder = async (name: string) => {
    try {
      const folder = await createFolder(name)
      setFolders((prev) => [...prev, folder])
      setActiveFolderId(folder.id)
      notify(`已创建「${folder.name}」`)
      return true
    } catch (error) {
      notify(errorText(error), 'err')
      return false
    }
  }

  const handleRenameFolder = async (folderId: string, name: string) => {
    try {
      const folder = await renameFolder(folderId, name)
      setFolders((prev) =>
        prev.map((item) => (item.id === folder.id ? folder : item)),
      )
      notify('文件夹已重命名')
      return true
    } catch (error) {
      notify(errorText(error), 'err')
      return false
    }
  }

  const handleDeleteFolder = async (folder: Folder) => {
    const count = folderCounts.get(folder.id) ?? 0
    if (
      !window.confirm(
        `删除文件夹「${folder.name}」？里面的 ${count} 篇论文会回到全部文献，不会删除论文。`,
      )
    ) {
      return
    }
    const previousFolders = folders
    const previousPapers = papers
    const previousActive = activeFolderId
    setFolders((prev) => prev.filter((item) => item.id !== folder.id))
    setPapers((prev) =>
      prev.map((paper) =>
        paper.folder_id === folder.id ? { ...paper, folder_id: null } : paper,
      ),
    )
    if (activeFolderId === folder.id) setActiveFolderId(null)
    try {
      const result = await deleteFolder(folder.id)
      notify(
        `已删除「${folder.name}」${
          result.moved ? `，${result.moved} 篇论文回到全部文献` : ''
        }`,
      )
    } catch (error) {
      setFolders(previousFolders)
      setPapers(previousPapers)
      setActiveFolderId(previousActive)
      notify(errorText(error), 'err')
    }
  }

  const movePaper = useCallback(
    async (paperId: string, folderId: string | null) => {
      const paper = papers.find((item) => item.id === paperId)
      if (!paper) return false
      const previous = paper.folder_id ?? null
      if (previous === folderId) return true
      setPapers((prev) =>
        prev.map((item) =>
          item.id === paperId ? { ...item, folder_id: folderId } : item,
        ),
      )
      try {
        await movePaperToFolder(paperId, folderId)
        notify(
          folderId
            ? `已移入「${folders.find((f) => f.id === folderId)?.name ?? '文件夹'}」`
            : '已移出到全部文献',
        )
        return true
      } catch (error) {
        setPapers((prev) =>
          prev.map((item) =>
            item.id === paperId ? { ...item, folder_id: previous } : item,
          ),
        )
        notify(errorText(error), 'err')
        return false
      }
    },
    [papers, folders, notify],
  )

  const cancelDwell = useCallback(() => {
    if (dwellTimer.current !== undefined) {
      window.clearTimeout(dwellTimer.current)
      dwellTimer.current = undefined
    }
    setDwellActive(false)
  }, [])

  const completeDragOut = useCallback(async () => {
    if (!dragPaper?.folder_id || movingOut.current) return
    movingOut.current = true
    const paper = dragPaper
    const moved = await movePaper(paper.id, null)
    movingOut.current = false
    if (!moved) {
      setDwellActive(false)
      return
    }
    setDragPaper((prev) => (prev ? { ...prev, folder_id: null } : prev))
    if (activeFolderId) postDragFolder.current = null
    setDragOutDone(true)
    window.setTimeout(() => setDragOutDone(false), 350)
  }, [dragPaper, activeFolderId, movePaper])

  const startDwell = useCallback(() => {
    if (
      !dragPaper?.folder_id ||
      dwellTimer.current !== undefined ||
      dragOutDone ||
      movingOut.current
    ) {
      return
    }
    setDwellActive(true)
    dwellTimer.current = window.setTimeout(() => {
      dwellTimer.current = undefined
      void completeDragOut()
    }, 700)
  }, [dragPaper, dragOutDone, completeDragOut])

  const handlePaperDragStart = (paper: PaperRow) => {
    suppressClick.current = true
    setDragPaper(paper)
    setDragOverFolderId(null)
    setDragOutDone(false)
    postDragFolder.current = undefined
    setDragFolderName(
      folders.find((folder) => folder.id === paper.folder_id)?.name ?? '',
    )
  }

  const handlePaperDragEnd = () => {
    cancelDwell()
    setDragPaper(null)
    setDragOverFolderId(null)
    setDragOutDone(false)
    const target = postDragFolder.current
    postDragFolder.current = undefined
    if (target !== undefined) setActiveFolderId(target)
    window.setTimeout(() => {
      suppressClick.current = false
    }, 180)
  }

  const handleDropOnFolder = (folderId: string) => {
    const paper = dragPaper
    setDragOverFolderId(null)
    if (!paper) return
    if ((paper.folder_id ?? null) === folderId) return
    const previousActive = activeFolderId
    if (activeFolderId && activeFolderId !== folderId) {
      postDragFolder.current = folderId
    }
    void movePaper(paper.id, folderId).then((moved) => {
      if (!moved && previousActive) setActiveFolderId(previousActive)
    })
  }

  const openPaper = (id: string) => {
    if (suppressClick.current) return
    onOpenPaper(id)
  }

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase()
    let rows = papers.filter((p) => {
      if (
        activeFolderId &&
        p.folder_id !== activeFolderId &&
        p.id !== dragPaper?.id
      ) {
        return false
      }
      if (q) {
        const hay = `${p.title} ${p.authors} ${p.filename}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      if (filter === 'ready') return p.status === 'ready'
      if (filter === 'untranslated') return p.translated === 0 && p.status === 'ready'
      if (filter === 'failed') return p.status === 'failed'
      return true
    })
    rows = [...rows].sort((a, b) => {
      if (sort === 'title') return a.title.localeCompare(b.title, 'zh')
      if (sort === 'pages') return b.pages - a.pages
      return b.created_at - a.created_at
    })
    return rows
  }, [papers, query, filter, sort, activeFolderId, dragPaper?.id])

  return (
    <div
      className="library"
      onDragOver={(e) => {
        if (!Array.from(e.dataTransfer.types).includes('Files')) return
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => {
        if (dragOver) setDragOver(false)
      }}
      onDrop={(e) => {
        if (e.dataTransfer.files.length === 0) return
        e.preventDefault()
        setDragOver(false)
        void doUpload(Array.from(e.dataTransfer.files))
      }}
    >
      <Sidebar
        active="library"
        count={papers.length}
        onNav={onNav}
        onSettings={() => setSettingsOpen(true)}
        folderSection={
          <LibraryFolders
            folders={folderRows}
            totalCount={papers.length}
            activeFolderId={activeFolderId}
            loading={foldersLoading}
            error={folderError}
            dragPaperId={dragPaper?.id ?? null}
            dragOverFolderId={dragOverFolderId}
            onSelect={setActiveFolderId}
            onCreate={handleCreateFolder}
            onRename={handleRenameFolder}
            onDelete={(folder) => void handleDeleteFolder(folder)}
            onDragEnterFolder={setDragOverFolderId}
            onDragLeaveFolder={(folderId) =>
              setDragOverFolderId((prev) => (prev === folderId ? null : prev))
            }
            onDropFolder={handleDropOnFolder}
          />
        }
      />

      <main className="library-main">
        <header className="library-head">
          <div className="library-title">
            {selectedFolder && (
              <button
                className="library-crumb"
                onClick={() => setActiveFolderId(null)}
                title="返回全部文献"
              >
                <IconBack />
                全部文献
              </button>
            )}
            <h1>{selectedFolder?.name ?? '文献库'}</h1>
            {selectedFolder && (
              <span className="library-count">{folderPapers.length} 篇</span>
            )}
          </div>
          <div className="library-head-actions">
            <button
              className="btn ghost"
              onClick={() => setZoteroOpen(true)}
              title="从本机 Zotero 选择带 PDF 的条目导入"
            >
              从 Zotero 导入
            </button>
            <button className="btn primary" onClick={() => inputRef.current?.click()} disabled={uploading}>
              <IconUpload /> {uploading ? '上传中…' : '添加文章'}
            </button>
            <input
              ref={inputRef}
              type="file"
              accept="application/pdf"
              multiple
              hidden
              onChange={(e) => {
                void doUpload(Array.from(e.target.files ?? []))
                e.target.value = ''
              }}
            />
          </div>
        </header>

        {demoMode && (
          <div className="library-notice">
            本地服务未启动，当前展示内置示例。
            启动方式：<code>python -m server.main</code>（另开终端）
          </div>
        )}

        <div className="library-search">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索标题、作者或文件名…"
          />
          <span className="search-count">{visible.length} 篇</span>
        </div>

        <div className="library-toolbar">
          <label className="tool">
            <span>筛选</span>
            <select value={filter} onChange={(e) => setFilter(e.target.value as Filter)}>
              <option value="all">全部</option>
              <option value="ready">已就绪</option>
              <option value="untranslated">未翻译</option>
              <option value="failed">解析失败</option>
            </select>
          </label>
          <label className="tool">
            <span>排序</span>
            <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
              <option value="recent">最近添加</option>
              <option value="title">标题</option>
              <option value="pages">页数</option>
            </select>
          </label>
          <div className="tool view-toggle" role="group" aria-label="视图">
            <button data-active={view === 'grid'} onClick={() => setView('grid')} title="网格">
              <IconGrid />
            </button>
            <button data-active={view === 'list'} onClick={() => setView('list')} title="列表">
              <IconRows />
            </button>
          </div>
          <button className="tool icon-only" onClick={() => setSettingsOpen(true)} title="设置">
            <IconSettings />
          </button>
        </div>

        {loading ? (
          <div className="library-empty">正在加载…</div>
        ) : visible.length === 0 ? (
          <div className="library-empty">
            {papers.length === 0 ? (
              <>
                <div className="dropzone" onClick={() => inputRef.current?.click()}>
                  <IconUpload />
                  <b>把 PDF 拖到这里，或点击上传</b>
                  <span>支持多选。上传后会自动解析版面、图表和公式，随后可一键翻译。</span>
                </div>
              </>
            ) : selectedFolder && folderPapers.length === 0 ? (
              <div className="folder-empty">
                <IconFolder />
                <b>这个文件夹还是空的</b>
                <span>把论文拖到左侧文件夹里</span>
                <button className="btn primary" onClick={() => inputRef.current?.click()}>
                  添加文章
                </button>
              </div>
            ) : (
              <span>没有符合条件的文章</span>
            )}
          </div>
        ) : (
          <div
            className={`paper-grid ${view === 'list' ? 'is-list' : ''} ${
              gridAnimating ? 'is-switching' : ''
            }`}
          >
            {visible.map((p) => (
              <PaperCard
                key={p.id}
                paper={p}
                view={view}
                onOpen={openPaper}
                onDelete={onDelete}
                onReparse={onReparse}
                draggable={!demoMode}
                dragging={dragPaper?.id === p.id}
                onDragStart={handlePaperDragStart}
                onDragEnd={handlePaperDragEnd}
              />
            ))}
            <button
              className="paper-card add-card"
              onClick={() => inputRef.current?.click()}
              title="添加文章"
            >
              <IconUpload />
              <span>添加文章</span>
            </button>
          </div>
        )}

        {visible.length > 0 && <p className="library-end">- 没有更多文章了 -</p>}
      </main>

      {dragOver && (
        <div className="drop-overlay">
          <div>
            <IconUpload />
            松开即可上传 PDF
          </div>
        </div>
      )}

      {toast && (
        <div className={`toast ${toast.kind}`} onClick={() => setToast(null)}>
          {toast.text}
        </div>
      )}

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={() => notify('模型配置已更新')}
      />

      <ZoteroImportDialog
        open={zoteroOpen}
        onClose={() => setZoteroOpen(false)}
        onImported={(imported, skipped) => {
          void refresh()
          if (imported > 0) {
            notify(
              `已从 Zotero 导入 ${imported} 篇${
                skipped ? `，跳过 ${skipped} 篇` : ''
              }${activeFolderId ? '到全部文献（未自动归档）' : ''}，正在解析…`,
            )
          } else {
            notify(skipped ? '所选条目都无法导入（无 PDF 或已在库中）' : '没有导入任何条目', 'err')
          }
        }}
      />
      {dragPaper && (dragPaper.folder_id || dragOutDone) && (
        <div
          className="folder-out-rail"
          data-open={true}
          data-done={dragOutDone}
          data-dwelling={dwellActive}
          onDragEnter={(event) => {
            event.preventDefault()
            startDwell()
          }}
          onDragOver={(event) => {
            event.preventDefault()
            event.dataTransfer.dropEffect = 'move'
            startDwell()
          }}
          onDragLeave={(event) => {
            if (event.currentTarget.contains(event.relatedTarget as Node)) return
            cancelDwell()
          }}
          onDrop={(event) => {
            event.preventDefault()
            cancelDwell()
          }}
        >
          <span className="folder-out-icon">
            <IconFolder />
          </span>
          <b>{dragOutDone ? '已移出' : '全部文献'}</b>
          <span className="folder-out-sub">
            {dragOutDone ? '论文已回到全部文献' : `从「${dragFolderName}」移出`}
          </span>
          {!dragOutDone && <small>停留 0.7 秒移出</small>}
          <span className="folder-out-progress">
            <i
              style={{
                width: dwellActive ? '100%' : '0%',
                transition: dwellActive
                  ? 'width 0.7s linear'
                  : 'width 0.12s ease',
              }}
            />
          </span>
        </div>
      )}
    </div>
  )
}
