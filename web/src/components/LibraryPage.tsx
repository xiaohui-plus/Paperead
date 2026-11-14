import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  DEMO_PAPER,
  createFolder,
  deleteFolder,
  deletePaper,
  importPaperByPath,
  listFolders,
  listPapers,
  movePaperToFolder,
  renameFolder,
  reparsePaper,
  uploadPapers,
  type Folder,
  type PaperRow,
} from '../lib/library'
import { toPayload, visionModel } from '../lib/settings'
import { PaperCard } from './PaperCard'
import { Sidebar } from './Sidebar'
import type { NavKey } from './Sidebar'
import { SettingsDialog } from './SettingsDialog'
import { FileSyncDialog } from './FileSyncDialog'

/** 上传/重新解析时把当前识图模型一并交给服务端，顺便让解析阶段直接产出 LaTeX。 */
function visionPayload() {
  const model = visionModel()
  return model ? toPayload(model) : null
}
import {
  IconBack,
  IconFolder,
  IconGrid,
  IconRefresh,
  IconRows,
  IconSettings,
  IconUpload,
} from './icons'
import { LibraryFolders } from './LibraryFolders'
import { tourBus } from '../tour/tourBus'
import { isMobileShell } from '../lib/mobile'
import { isDesktopApp } from '../lib/update'
import {
  deleteOffline,
  downloadPaper,
  listOffline,
  type OfflineSummary,
} from '../lib/offlineStore'
import { importStoryFile } from '../lib/storyImport'
import { importSyncPackage } from '../lib/deviceSync'
import { SCHOLAREAD_SYNCED } from '../lib/scholareadSync'

type Filter = 'all' | 'ready' | 'reading' | 'unread' | 'finished' | 'untranslated' | 'failed'
type Sort = 'recent' | 'read' | 'title' | 'pages'

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
  const [zoteroSyncOpen, setZoteroSyncOpen] = useState(false)
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
  const dragDepth = useRef(0)
  const movingOut = useRef(false)
  const postDragFolder = useRef<string | null | undefined>(undefined)
  const suppressClick = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const storyInputRef = useRef<HTMLInputElement>(null)
  const mobile = isMobileShell()
  const [offlineItems, setOfflineItems] = useState<OfflineSummary[]>([])
  const [offlineMode, setOfflineMode] = useState(false)
  const [downloads, setDownloads] = useState<Record<string, { done: number; total: number }>>({})
  const [importing, setImporting] = useState(false)

  const [syncImporting, setSyncImporting] = useState(false)
  const syncInputRef = useRef<HTMLInputElement>(null)

  const errorText = (error: unknown) =>
    error instanceof Error ? error.message : '操作失败，请重试'

  const notify = useCallback((text: string, kind: 'ok' | 'err' = 'ok') => {
    setToast({ text, kind })
    window.setTimeout(() => setToast(null), 4200)
  }, [])

  const refresh = useCallback(async () => {
    let serverOk = false
    try {
      const rows = await listPapers()
      setPapers(rows)
      setDemoMode(false)
      serverOk = true
      try {
        setFolders(await listFolders())
        setFolderError(null)
      } catch (error) {
        setFolderError(errorText(error))
      }
    } catch {
      // 手机离开电脑后：不塞示例，改由下面的本机离线库撑起页面
      setPapers(isMobileShell() ? [] : [DEMO_PAPER])
      setDemoMode(!isMobileShell())
      setFolders([])
      setFolderError(null)
    } finally {
      setLoading(false)
      setFoldersLoading(false)
    }
    setOfflineMode(!serverOk)
    try {
      setOfflineItems(await listOffline())
    } catch {
      setOfflineItems([])
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 学术阅读同步导入新论文后，文献库自动刷新
  useEffect(() => {
    const onSynced = () => void refresh()
    window.addEventListener(SCHOLAREAD_SYNCED, onSynced)
    return () => window.removeEventListener(SCHOLAREAD_SYNCED, onSynced)
  }, [refresh])

  // 有解析/翻译中的任务时轮询
  useEffect(() => {
    if (demoMode) return
    const active = papers.some((p) => p.status === 'parsing')
    if (!active) return
    const timer = window.setInterval(() => void refresh(), 1800)
    return () => window.clearInterval(timer)
  }, [papers, demoMode, refresh])

  const doImportSync = async (file: File | null) => {
    if (!file) return
    setSyncImporting(true)
    try {
      const report = await importSyncPackage(file)
      await refresh()
      if (report.failed.length > 0) {
        notify(
          `导入 ${report.imported + report.updated} 篇，${report.failed.length} 篇失败`,
          'err',
        )
      } else if (report.updated > 0) {
        notify(`已导入 ${report.imported} 篇，覆盖 ${report.updated} 篇`)
      } else {
        notify(`已导入 ${report.imported} 篇`)
      }
    } catch (error) {
      notify(`导入同步包失败：${errorText(error)}`, 'err')
    } finally {
      setSyncImporting(false)
    }
  }

  const doUpload = useCallback(
    async (files: File[]) => {
      if (offlineMode) {
        notify('当前连不上电脑端，暂时不能上传新论文', 'err')
        return
      }
      const pdfs = files.filter((f) => f.name.toLowerCase().endsWith('.pdf'))
      if (!pdfs.length) {
        notify('只支持 PDF 文件', 'err')
        return
      }
      setUploading(true)
      try {
        const res = await uploadPapers(pdfs, activeFolderId, visionPayload())
        await refresh()
        if (res.added.length > 0) {
          tourBus.emit('library:uploaded', { paperId: res.added[0].id })
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
    },
    [offlineMode, activeFolderId, notify, refresh, onOpenPaper],
  )

  // 桌面壳把外部文件拖放交给 Tauri，WebView 收不到 HTML5 drop，
  // 只能订阅原生 drag-drop 事件拿本地路径，再让后端从磁盘导入。
  const doImportPaths = useCallback(
    async (paths: string[]) => {
      if (uploading) {
        notify('正在上传，请稍候再拖入', 'err')
        return
      }
      if (offlineMode) {
        notify('当前连不上电脑端，暂时不能上传新论文', 'err')
        return
      }
      const pdfs = paths.filter((path) => path.toLowerCase().endsWith('.pdf'))
      if (!pdfs.length) {
        notify('只支持 PDF 文件', 'err')
        return
      }
      setUploading(true)
      const added: PaperRow[] = []
      const failures: string[] = []
      try {
        for (const path of pdfs) {
          try {
            const res = await importPaperByPath(path, activeFolderId, visionPayload())
            added.push(...res.added)
            for (const item of res.failed) {
              failures.push(`${item.filename}：${item.error}`)
            }
          } catch (error) {
            failures.push(`${path.split(/[\\/]/).pop() ?? path}：${errorText(error)}`)
          }
        }
        await refresh()
        if (added.length > 0) {
          tourBus.emit('library:uploaded', { paperId: added[0].id })
          notify(
            failures.length
              ? `已添加 ${added.length} 篇，${failures.length} 篇失败：${failures[0]}`
              : `已添加 ${added.length} 篇，正在解析…`,
            failures.length ? 'err' : 'ok',
          )
          onOpenPaper(added[0].id)
        } else {
          notify(`上传失败：${failures[0] ?? '未知错误'}`, 'err')
        }
      } finally {
        setUploading(false)
      }
    },
    [uploading, offlineMode, activeFolderId, notify, refresh, onOpenPaper],
  )

  useEffect(() => {
    if (!isDesktopApp()) return
    let disposed = false
    let unlisten: (() => void) | undefined
    void (async () => {
      try {
        const { getCurrentWebview } = await import('@tauri-apps/api/webview')
        const stop = await getCurrentWebview().onDragDropEvent((event) => {
          const payload = event.payload
          if (payload.type === 'enter') {
            setDragOver(true)
          } else if (payload.type === 'over') {
            setDragOver(true)
          } else if (payload.type === 'leave') {
            setDragOver(false)
          } else if (payload.type === 'drop') {
            setDragOver(false)
            void doImportPaths(payload.paths)
          }
        })
        if (disposed) stop()
        else unlisten = stop
      } catch {
        // 旧壳或浏览器预览：保持 HTML5 / 选择文件上传可用
      }
    })()
    return () => {
      disposed = true
      unlisten?.()
    }
  }, [doImportPaths])

  // 整个窗口都接文件拖放：从资源管理器拖 PDF 进来即可上传。
  // 用 dragenter/dragleave 计数而不是逐个子元素判断，避免浮层闪烁或被子元素吞掉。
  useEffect(() => {
    if (isDesktopApp()) return
    const hasFiles = (event: DragEvent) =>
      Array.from(event.dataTransfer?.types ?? []).includes('Files')

    const onDragEnter = (event: DragEvent) => {
      if (!hasFiles(event)) return
      event.preventDefault()
      dragDepth.current += 1
      setDragOver(true)
    }

    const onDragOver = (event: DragEvent) => {
      if (!hasFiles(event)) return
      event.preventDefault()
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
      setDragOver(true)
    }

    const onDragLeave = () => {
      dragDepth.current = Math.max(0, dragDepth.current - 1)
      if (dragDepth.current === 0) setDragOver(false)
    }

    const onDrop = (event: DragEvent) => {
      const files = Array.from(event.dataTransfer?.files ?? [])
      dragDepth.current = 0
      setDragOver(false)
      if (!files.length) return
      event.preventDefault()
      if (uploading) {
        notify('正在上传，请稍候再拖入', 'err')
        return
      }
      void doUpload(files)
    }

    window.addEventListener('dragenter', onDragEnter, true)
    window.addEventListener('dragover', onDragOver, true)
    window.addEventListener('dragleave', onDragLeave, true)
    window.addEventListener('drop', onDrop, true)
    return () => {
      window.removeEventListener('dragenter', onDragEnter, true)
      window.removeEventListener('dragover', onDragOver, true)
      window.removeEventListener('dragleave', onDragLeave, true)
      window.removeEventListener('drop', onDrop, true)
    }
  }, [doUpload, uploading, notify])

  const onDelete = async (paper: PaperRow) => {
    // 本机副本：只删手机上的，服务器上的论文不受影响
    if (paper.offline && offlineMode) {
      const label = paper.local_source === 'storyreader' ? '导入的书' : '本机副本'
      if (!window.confirm(`删除《${paper.title}》的${label}？电脑上的原文件不受影响。`)) return
      try {
        await deleteOffline(paper.id)
        await refresh()
        notify('已删除本机副本')
      } catch (e) {
        notify(errorText(e), 'err')
      }
      return
    }
    if (!window.confirm(`删除《${paper.title}》？`)) return
    try {
      if (!paper.offline) await deletePaper(paper.id)
      // 服务器可达时一并清掉本机副本，避免下次仍能离线打开
      await deleteOffline(paper.id).catch(() => undefined)
      await refresh()
      notify('已删除')
    } catch (e) {
      notify(e instanceof Error ? e.message : '删除失败', 'err')
    }
  }

  /** 把论文（解析结果 + 各页图片）下载到本机，离开电脑后仍可阅读。 */
  const onDownload = async (paper: PaperRow) => {
    setDownloads((prev) => ({ ...prev, [paper.id]: { done: 0, total: 1 } }))
    try {
      await downloadPaper(paper.id, (progress) => {
        setDownloads((prev) => ({
          ...prev,
          [paper.id]: { done: progress.done, total: Math.max(progress.total, 1) },
        }))
      })
      await refresh()
      notify('已下载到本机，离线也能阅读')
    } catch (e) {
      notify(e instanceof Error ? e.message : '下载失败', 'err')
    } finally {
      setDownloads((prev) => {
        const next = { ...prev }
        delete next[paper.id]
        return next
      })
    }
  }

  const onStoryFiles = async (files: File[]) => {
    if (!files.length) return
    setImporting(true)
    let imported = 0
    let failure = ''
    for (const file of files) {
      try {
        await importStoryFile(file)
        imported += 1
      } catch (e) {
        failure = e instanceof Error ? e.message : '导入失败'
      }
    }
    await refresh()
    setImporting(false)
    if (imported) notify(`已从 StoryReader 导入 ${imported} 个文件`)
    else notify(failure || '没有可导入的内容', 'err')
  }

  const onReparse = async (paper: PaperRow) => {
    try {
      await reparsePaper(paper.id, visionPayload())
      notify('已重新开始解析')
      await refresh()
    } catch (e) {
      notify(e instanceof Error ? e.message : '解析失败', 'err')
    }
  }

  // 服务器列表 + 本机离线库：下载过的论文合并成一条，导入的书单独补进来
  const allPapers = useMemo(() => {
    const localById = new Map(offlineItems.map((item) => [item.id, item]))
    const rows: PaperRow[] = papers.map((paper) => {
      const local = localById.get(paper.id)
      return local
        ? { ...paper, offline: true, local_source: local.source }
        : paper
    })
    const seen = new Set(papers.map((paper) => paper.id))
    for (const item of offlineItems) {
      if (seen.has(item.id)) continue
      rows.push({
        id: item.id,
        title: item.title || '未命名',
        authors: item.authors,
        filename: '',
        pages: item.pages,
        size: item.bytes,
        created_at: item.updatedAt,
        status: 'ready',
        error: '',
        translated: 0,
        blocks: 0,
        thumb_url: null,
        file_url: '',
        offline: true,
        local_source: item.source,
      })
    }
    return rows
  }, [papers, offlineItems])

  const folderCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const paper of allPapers) {
      if (paper.folder_id) {
        counts.set(paper.folder_id, (counts.get(paper.folder_id) ?? 0) + 1)
      }
    }
    return counts
  }, [allPapers])

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
        ? allPapers.filter((paper) => paper.folder_id === activeFolderId)
        : allPapers,
    [allPapers, activeFolderId],
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
    let rows = allPapers.filter((p) => {
      if (
        activeFolderId &&
        p.folder_id !== activeFolderId &&
        p.id !== dragPaper?.id
      ) {
        return false
      }
      if (q) {
        const hay = `${p.title} ${p.authors} ${p.filename ?? ''}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      if (filter === 'ready') return p.status === 'ready'
      if (filter === 'reading') {
        const pct = p.read_pct ?? 0
        return pct > 0 && pct < 95
      }
      if (filter === 'unread') return !p.read_pct
      if (filter === 'finished') return (p.read_pct ?? 0) >= 95
      if (filter === 'untranslated') return p.translated === 0 && p.status === 'ready'
      if (filter === 'failed') return p.status === 'failed'
      return true
    })
    rows = [...rows].sort((a, b) => {
      if (sort === 'title') return a.title.localeCompare(b.title, 'zh')
      if (sort === 'pages') return b.pages - a.pages
      if (sort === 'read') return (b.read_at ?? 0) - (a.read_at ?? 0)
      return b.created_at - a.created_at
    })
    return rows
  }, [allPapers, query, filter, sort, activeFolderId, dragPaper?.id])

  return (
    <div className="library">
      <Sidebar
        active="library"
        count={allPapers.length}
        onNav={onNav}
        onSettings={() => setSettingsOpen(true)}
        folderSection={
          <LibraryFolders
            folders={folderRows}
            totalCount={allPapers.length}
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
            {!mobile && (
              <button
                className="btn ghost"
                data-tour="zotero-sync"
                onClick={() => setZoteroSyncOpen(true)}
                title="与 Zotero 云端双向同步文献、分类和 PDF"
              >
                Zotero 同步
              </button>
            )}

            {mobile && (
              <button
                className="btn ghost"
                onClick={() => syncInputRef.current?.click()}
                disabled={syncImporting}
                title="导入电脑导出的 .paperead 同步包"
              >
                {syncImporting ? '导入中…' : '导入同步包'}
              </button>
            )}
            {mobile && (
              <input
                ref={syncInputRef}
                type="file"
                accept=".paperead,.zip,application/zip"
                hidden
                onChange={(event) => {
                  const file = event.target.files?.[0] ?? null
                  event.target.value = ''
                  void doImportSync(file)
                }}
              />
            )}
            {mobile && (
              <button
                className="btn ghost"
                onClick={() => storyInputRef.current?.click()}
                disabled={importing}
                title="导入 StoryReader 导出的 JSON / TXT / Markdown"
              >
                {importing ? '导入中…' : '从 StoryReader 导入'}
              </button>
            )}
            <input
              ref={storyInputRef}
              type="file"
              accept=".json,.txt,.md,.markdown,application/json,text/plain"
              multiple
              hidden
              onChange={(e) => {
                void onStoryFiles(Array.from(e.target.files ?? []))
                e.target.value = ''
              }}
            />

            <button
              className="btn primary"
              data-tour="library-upload"
              onClick={() => inputRef.current?.click()}
              disabled={uploading || offlineMode}
              title={offlineMode ? '连上电脑端后才能上传新论文' : undefined}
            >
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

        {offlineMode && (
          <div className="library-notice">
            已进入本机模式：连不上电脑端，这里显示已下载和导入的内容。
            翻译和问答会直接调用你配置的模型（需要联网）。
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
              <option value="reading">在读</option>
              <option value="unread">未读</option>
              <option value="finished">已读完</option>
              <option value="ready">已就绪</option>
              <option value="untranslated">未翻译</option>
              <option value="failed">解析失败</option>
            </select>
          </label>
          <label className="tool">
            <span>排序</span>
            <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
              <option value="recent">最近添加</option>
              <option value="read">最近阅读</option>
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
          {mobile && (
            <button
              className="tool icon-only"
              onClick={() => setZoteroSyncOpen(true)}
              title="Zotero 双向同步"
            >
              <IconRefresh />
            </button>
          )}
        </div>

        {loading ? (
          <div className="library-empty">正在加载…</div>
        ) : visible.length === 0 ? (
          <div className="library-empty">
            {allPapers.length === 0 ? (
              offlineMode ? (
                <div className="folder-empty">
                  <IconFolder />
                  <b>本机还没有内容</b>
                  <span>连上电脑后把论文「下载」到本机；也可以从 StoryReader 导入。</span>
                </div>
              ) : (
                <div className="dropzone" onClick={() => inputRef.current?.click()}>
                  <IconUpload />
                  <b>把 PDF 拖到这里，或点击上传</b>
                  <span>支持多选。上传后会自动解析版面、图表和公式，随后可一键翻译。</span>
                </div>
              )
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
                draggable={!demoMode && !offlineMode && !p.offline}
                dragging={dragPaper?.id === p.id}
                onDragStart={handlePaperDragStart}
                onDragEnd={handlePaperDragEnd}
                downloadProgress={downloads[p.id] ?? null}
                onDownload={offlineMode ? undefined : onDownload}
              />
            ))}
            {!offlineMode && (
              <button
                className="paper-card add-card"
                onClick={() => inputRef.current?.click()}
                title="添加文章"
              >
                <IconUpload />
                <span>添加文章</span>
              </button>
            )}
          </div>
        )}

        {visible.length > 0 && <p className="library-end">- 没有更多文章了 -</p>}
      </main>

      {dragOver && (
        <div className="drop-overlay">
          <div className="drop-overlay-card">
            <IconUpload />
            <b>松开即可上传 PDF</b>

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
        onClose={() => {
          setSettingsOpen(false)
          // 设置里可能刚联机并拉取了文献，关闭时刷新本机离线库
          void refresh()
        }}
        onSaved={() => notify('模型配置已更新')}
        onLibraryChanged={() => void refresh()}
      />

      <FileSyncDialog
        open={zoteroSyncOpen}
        initialTarget="zotero"
        onClose={() => setZoteroSyncOpen(false)}
        onSynced={() => {
          void refresh()
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
