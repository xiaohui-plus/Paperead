import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import {
  IconBack,
  IconChat,
  IconClose,
  IconCopy,
  IconDownload,
  IconEdit,
  IconFolder,
  IconPlus,
  IconTrash,
} from '../components/icons'
import { open as openDialog } from '@tauri-apps/plugin-dialog'
import { Markdown } from '../components/Markdown'
import { FileTypeIcon } from '../components/fileTypeIcons'
import { SettingsDialog } from '../components/SettingsDialog'
import {
  relativeTime,
  researchApi,
  type ArtifactSummary,
  type FsEntry,
  type ProjectFile,
  type ProjectKind,
  type ProjectSummary,
  type SessionSummary,
} from '../lib/researchApi'
import { isDesktopApp } from '../lib/update'
import { Composer } from './Composer'
import { LocalFolderBrowser } from './LocalFolderBrowser'
import { ProjectExplorer } from './ProjectExplorer'
import { ProjectFileTree } from './ProjectFileTree'
import { FilePreview, LocalFilePreview } from './preview/FilePreview'
import { SessionView } from './SessionView'
import {
  dropProjectCache,
  readProjectCache,
  writeProjectCache,
} from './researchCache'
import { tourBus } from '../tour/tourBus'

interface Props {
  projectId: string
  sessionId: string | null
  initialRename: boolean
  /** 首页或快捷动作带来的首条消息，交给新建的会话发送一次。 */
  initialText?: string | null
  onInitialTextConsumed?: () => void
  onBack: (kind: ProjectKind) => void
  onSelectSession: (sessionId: string | null) => void
}

type Tab = 'files' | 'sessions'

const RAIL_MIN = 220
const RAIL_MAX = 520
const RAIL_DEFAULT = 272
const RAIL_STORAGE_KEY = 'paperead:project-rail-width'

function clampRail(width: number): number {
  return Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(width)))
}

function initialRailWidth(): number {
  try {
    const saved = Number(localStorage.getItem(RAIL_STORAGE_KEY))
    if (Number.isFinite(saved) && saved > 0) return clampRail(saved)
  } catch {
    /* localStorage 不可用时用默认宽度 */
  }
  return RAIL_DEFAULT
}

/** 产物在资源管理器里挂在虚拟「产物」文件夹下，与普通文件共用同一棵树。 */
const ARTIFACT_FOLDER_ID = 'artifact-root'
const artifactNodeId = (artifactId: string) => `artifact:${artifactId}`

/** 取路径里的扩展名（含点），没有则返回空串。 */
function extensionOf(path: string): string {
  const base = path.split('/').pop() || ''
  const index = base.lastIndexOf('.')
  return index > 0 && index < base.length - 1 ? base.slice(index) : ''
}

/** 资源管理器里的文件都要带后缀；标题缺后缀时用磁盘路径补上。 */
function nameWithSuffix(name: string, path: string): string {
  const ext = extensionOf(path)
  if (!ext) return name
  return name.toLowerCase().includes(ext.toLowerCase()) ? name : `${name}${ext}`
}

function withSuffix(item: ProjectFile): ProjectFile {
  if (item.kind === 'folder') return item
  return { ...item, name: nameWithSuffix(item.name, item.path) }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请重试'
}

/** 项目页：全局侧栏在此隐藏，左栏为文件 / 会话。 */
export function ProjectPage({
  projectId,
  sessionId,
  initialRename,
  initialText,
  onInitialTextConsumed,
  onBack,
  onSelectSession,
}: Props) {
  /** 上次离开时的项目快照：命中的话项目页与会话正文都能直接显示。 */
  const snapshot = useRef(readProjectCache(projectId)).current
  const [project, setProject] = useState<ProjectSummary | null>(
    snapshot?.project ?? null,
  )
  const [files, setFiles] = useState<ProjectFile[]>(snapshot?.files ?? [])
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>(
    snapshot?.artifacts ?? [],
  )
  /** 产物目录的绝对路径：产物区是虚拟节点，路径要单独保存才能复制。 */
  const [artifactsRoot, setArtifactsRoot] = useState('')
  const [sessions, setSessions] = useState<SessionSummary[]>(
    snapshot?.sessions ?? [],
  )
  const [loading, setLoading] = useState(!snapshot)
  const [error, setError] = useState<string | null>(null)
  const [sessionTitle, setSessionTitle] = useState<string | null>(
    snapshot?.sessions.find((item) => item.id === sessionId)?.title ?? null,
  )

  const [tab, setTab] = useState<Tab>('sessions')
  // 手机端：文件/会话栏是抽屉，默认收起，把高度留给会话正文
  const [railOpen, setRailOpen] = useState(false)
  const [railWidth, setRailWidth] = useState(initialRailWidth)
  const resizeStart = useRef<{ x: number; width: number } | null>(null)
  const [preview, setPreview] = useState<ProjectFile | null>(null)
  const [previewArtifact, setPreviewArtifact] = useState<ArtifactSummary | null>(null)
  const [localPreview, setLocalPreview] = useState<{
    entry: FsEntry
    url: string
  } | null>(null)
  const [folderPicker, setFolderPicker] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [activeFolderId, setActiveFolderId] = useState<string | null>(null)
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [folderParent, setFolderParent] = useState<string | null | undefined>(undefined)
  const [folderName, setFolderName] = useState('')
  const [uploading, setUploading] = useState(false)

  const [renaming, setRenaming] = useState(false)
  const [draftName, setDraftName] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  const [sessionMenu, setSessionMenu] = useState<{
    session: SessionSummary
    x: number
    y: number
  } | null>(null)
  const [sessionRenaming, setSessionRenaming] = useState<string | null>(null)
  const [sessionDraft, setSessionDraft] = useState('')
  const [pendingText, setPendingText] = useState<string | null>(
    initialText ?? null,
  )
  const [sending, setSending] = useState(false)

  const renameRef = useRef<HTMLInputElement>(null)
  const uploadRef = useRef<HTMLInputElement>(null)
  const folderRef = useRef<HTMLInputElement>(null)

  // webkitdirectory 不在 React 的 input 类型里，挂载后手动加
  useEffect(() => {
    const el = folderRef.current
    if (!el) return
    el.setAttribute('webkitdirectory', '')
    el.setAttribute('directory', '')
  }, [])

  useEffect(() => {
    let alive = true
    if (!snapshot) setLoading(true)
    setError(null)
    Promise.all([
      researchApi.getProject(projectId),
      researchApi.listFiles(projectId),
      researchApi.listSessions(projectId),
      researchApi.listArtifacts(projectId),
    ])
      .then(([loadedProject, loadedFiles, loadedSessions, loadedArtifacts]) => {
        if (!alive) return
        setProject(loadedProject)
        setFiles(loadedFiles)
        setSessions(loadedSessions)
        setArtifacts(loadedArtifacts.items)
        setArtifactsRoot(loadedArtifacts.root)
        setDraftName(loadedProject.name)
        if (initialRename) setRenaming(true)
      })
      .catch((loadError) => {
        if (alive) setError(errorText(loadError))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [projectId, initialRename])

  // 切到别的会话时先用列表标题占位，正文加载后会由 SessionView 上报更准确
  // 的标题；列表后续的 running 更新不覆盖它。
  useEffect(() => {
    setSessionTitle(
      sessions.find((item) => item.id === sessionId)?.title ?? null,
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // 保留最近一次的项目快照，切回文献库/阅读页再回来时不必重新等接口。
  useEffect(() => {
    if (loading || !project) return
    writeProjectCache(projectId, { project, files, sessions, artifacts })
  }, [projectId, loading, project, files, sessions, artifacts])

  useEffect(() => {
    if (!renaming) return
    const input = renameRef.current
    if (input) {
      input.focus()
      input.select()
    }
  }, [renaming])

  useEffect(() => {
    tourBus.emit('project:tab-changed', { tab })
  }, [tab])

  useEffect(() => {
    if (!sessionMenu) return
    const close = (event: MouseEvent) => {
      if (!(event.target as Element).closest('.project-tree-menu')) {
        setSessionMenu(null)
      }
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSessionMenu(null)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', onKey)
    }
  }, [sessionMenu])

  useEffect(() => {
    if (!railOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setRailOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [railOpen])

  // 同一项目内再次从首页发起时，props 会给出新的首条消息。
  useEffect(() => {
    if (initialText) setPendingText(initialText)
  }, [initialText])

  // 刚创建的项目先让用户选择工作文件夹，取消则继续用项目默认文件夹。
  useEffect(() => {
    if (!initialRename || !project || project.rootPath) return
    setFolderPicker(true)
  }, [initialRename, project?.id, project?.rootPath])

  // agent 写产物不会触发前端刷新；切回「文件」时重新拉一次。
  const refreshArtifacts = useCallback(async () => {
    try {
      const [nextFiles, nextArtifacts] = await Promise.all([
        researchApi.listFiles(projectId),
        researchApi.listArtifacts(projectId),
      ])
      setFiles(nextFiles)
      setArtifacts(nextArtifacts.items)
      setArtifactsRoot(nextArtifacts.root)
    } catch {
      // 刷新失败保留旧列表即可，不打断用户。
    }
  }, [projectId])

  const selectedSession = sessions.find((item) => item.id === sessionId) ?? null

  // 产物与文件项存放在不同表里，这里合并成一棵树，保证资源管理器里都能看到。
  const artifactItems: ProjectFile[] =
    artifacts.length === 0
      ? []
      : [
          {
            id: ARTIFACT_FOLDER_ID,
            parentId: null,
            kind: 'folder',
            name: '产物',
            path: '',
            absolute: artifactsRoot,
            mime: '',
            size: 0,
            refId: null,
            meta: { readOnly: true, defaultOpen: true },
            addedAt: 0,
          },
          ...artifacts.map((artifact) =>
            withSuffix({
              id: artifactNodeId(artifact.id),
              parentId: ARTIFACT_FOLDER_ID,
              kind: 'artifact' as const,
              name: artifact.title,
              path: artifact.path,
              absolute: artifact.absolute,
              mime: '',
              size: 0,
              refId: artifact.id,
              meta: { readOnly: true, artifactKind: artifact.kind },
              addedAt: artifact.createdAt,
            }),
          ),
        ]
  const treeItems =
    artifactItems.length > 0
      ? [...files.map(withSuffix), ...artifactItems]
      : files.map(withSuffix)

  const workspaceLabel = (() => {
    const root = (project?.rootPath || '').replace(/[\\/]+$/, '')
    return root.split(/[\\/]/).filter(Boolean).pop() || project?.name || ''
  })()

  const back = () => onBack(project?.kind ?? 'research')

  const commitName = async () => {
    if (!project) {
      setRenaming(false)
      return
    }
    const name = draftName.trim() || '新项目'
    setRenaming(false)
    if (name === project.name) return
    try {
      const updated = await researchApi.renameProject(project.id, name)
      setProject(updated)
      setDraftName(updated.name)
    } catch (renameError) {
      setError(errorText(renameError))
    }
  }

  const archive = async () => {
    if (!project) return
    setMenuOpen(false)
    try {
      await researchApi.archiveProject(project.id)
      dropProjectCache(project.id)
      onBack(project.kind)
    } catch (archiveError) {
      setError(errorText(archiveError))
    }
  }

  const purge = async () => {
    if (!project) return
    setMenuOpen(false)
    const ok = window.confirm(
      `删除项目「${project.name}」？\n\n` +
        '该项目下的所有文件、会话和产物都会被永久删除，无法恢复。',
    )
    if (!ok) return
    try {
      await researchApi.purgeProject(project.id)
      dropProjectCache(project.id)
      onBack(project.kind)
    } catch (purgeError) {
      setError(errorText(purgeError))
    }
  }

  const openComposer = () => {
    setPreview(null)
    setPreviewArtifact(null)
    setLocalPreview(null)
    onSelectSession(null)
    setTab('sessions')
    setRailOpen(false)
  }

  const applyRoot = async (path: string) => {
    const updated = await researchApi.setProjectRoot(projectId, path)
    setProject(updated)
    setPreview(null)
    setPreviewArtifact(null)
    setLocalPreview(null)
    setActiveFolderId(null)
    setTab('files')
  }

  /** 桌面端用系统文件夹选择器；网页/Android 回退到内置目录浏览器。 */
  const pickWorkspaceFolder = async () => {
    setError(null)
    if (!isDesktopApp()) {
      setFolderPicker(true)
      return
    }
    try {
      const picked = await openDialog({
        directory: true,
        multiple: false,
        title: '选择工作文件夹',
      })
      if (typeof picked === 'string' && picked) {
        await applyRoot(picked)
      }
    } catch (pickError) {
      setError(errorText(pickError))
    }
  }

  const openLocalFile = (entry: FsEntry) => {
    setPreview(null)
    setPreviewArtifact(null)
    setRailOpen(false)
    setLocalPreview({
      entry,
      url: researchApi.projectExplorerFileUrl(projectId, entry.path),
    })
  }


  const send = async (text: string) => {
    const value = text.trim()
    if (!value || sending) return
    setSending(true)
    try {
      const session = await researchApi.createSession(
        projectId,
        value.slice(0, 40),
      )
      setSessions((prev) => [session, ...prev])
      setPendingText(value)
      setPreview(null)
      onSelectSession(session.id)
    } catch (sendError) {
      setError(errorText(sendError))
    } finally {
      setSending(false)
    }
  }

  const updateSessionMeta = useCallback(
    (patch: { title?: string; running?: boolean }) => {
      if (!sessionId) return
      setSessions((prev) =>
        prev.map((item) =>
          item.id === sessionId
            ? {
                ...item,
                title: patch.title ?? item.title,
                running: patch.running ?? item.running,
                status:
                  patch.running === undefined
                    ? item.status
                    : patch.running
                      ? 'running'
                      : 'idle',
                updatedAt:
                  patch.running === undefined
                    ? item.updatedAt
                    : Math.floor(Date.now() / 1000),
              }
            : item,
        ),
      )
    },
    [sessionId],
  )

  const startFolder = () => {
    setFolderParent(activeFolderId)
    setFolderName('')
    setTab('files')
  }

  const createFolder = async () => {
    const name = folderName.trim()
    if (!name) return
    try {
      const folder = await researchApi.createFolder(
        projectId,
        name,
        folderParent ?? null,
      )
      setFiles((prev) => [...prev, folder])
      setFolderParent(undefined)
      setFolderName('')
      setActiveFolderId(folder.id)
    } catch (folderError) {
      setError(errorText(folderError))
    }
  }

  const upload = async (list: FileList | null) => {
    if (!list || list.length === 0) return
    setUploading(true)
    try {
      const uploaded: ProjectFile[] = []
      for (const file of Array.from(list)) {
        uploaded.push(
          await researchApi.uploadFile(projectId, file, activeFolderId),
        )
      }
      setFiles((prev) => [...prev, ...uploaded])
    } catch (uploadError) {
      setError(errorText(uploadError))
    } finally {
      setUploading(false)
    }
  }

  const uploadFolder = async (list: FileList | null) => {
    if (!list || list.length === 0) return
    setUploading(true)
    try {
      const entries = Array.from(list).map((file) => ({
        file,
        path:
          (file as File & { webkitRelativePath?: string }).webkitRelativePath ||
          file.name,
      }))
      const result = await researchApi.uploadFolder(
        projectId,
        entries,
        activeFolderId,
      )
      applyItems(result.items)
      setTab('files')
      if (result.errors.length) {
        setError(`部分文件没有导入：${result.errors.join('；')}`)
      }
    } catch (uploadError) {
      setError(errorText(uploadError))
    } finally {
      setUploading(false)
    }
  }

  const applyItems = (next: ProjectFile[]) => {
    setFiles(next)
    setPreview((prev) => {
      if (!prev) return prev
      const found = next.find((item) => item.id === prev.id)
      return found ? withSuffix(found) : null
    })
    setActiveFolderId((prev) =>
      prev && next.some((item) => item.id === prev) ? prev : null,
    )
    setSelectedFiles((prev) =>
      prev.filter((id) => next.some((item) => item.id === id)),
    )
  }

  const renameFile = async (item: ProjectFile, name: string) => {
    try {
      applyItems(await researchApi.renameItem(projectId, item.id, name))
    } catch (renameError) {
      setError(errorText(renameError))
    }
  }

  const moveFile = async (item: ProjectFile, parentId: string | null) => {
    try {
      if (item.kind === 'artifact') {
        if (!item.refId) return
        applyItems(
          await researchApi.moveArtifact(projectId, item.refId, parentId),
        )
        setArtifacts((prev) => prev.filter((entry) => entry.id !== item.refId))
        setPreviewArtifact((prev) =>
          prev && prev.id === item.refId ? null : prev,
        )
        return
      }
      applyItems(await researchApi.moveItem(projectId, item.id, parentId))
    } catch (moveError) {
      setError(errorText(moveError))
    }
  }

  const copyFile = async (item: ProjectFile, parentId: string | null) => {
    try {
      if (item.kind === 'artifact') {
        if (!item.refId) return
        applyItems(
          await researchApi.copyArtifact(projectId, item.refId, parentId),
        )
        return
      }
      applyItems(await researchApi.copyItem(projectId, item.id, parentId))
    } catch (copyError) {
      setError(errorText(copyError))
    }
  }

  const deleteFile = async (item: ProjectFile) => {
    if (item.kind === 'artifact') {
      if (!item.refId) return
      if (!window.confirm(`确定删除产物「${item.name}」？文件会被删除，无法恢复。`)) {
        return
      }
      try {
        await researchApi.deleteArtifact(projectId, item.refId)
        setArtifacts((prev) => prev.filter((entry) => entry.id !== item.refId))
        setPreviewArtifact((prev) =>
          prev && prev.id === item.refId ? null : prev,
        )
      } catch (deleteError) {
        setError(errorText(deleteError))
      }
      return
    }
    const what = item.kind === 'folder' ? '文件夹及其中的所有文件' : '文件'
    if (!window.confirm(`确定删除${what}「${item.name}」？删除后无法恢复。`)) return
    try {
      applyItems(await researchApi.deleteItem(projectId, item.id))
    } catch (deleteError) {
      setError(errorText(deleteError))
    }
  }

  const addToLibrary = async (item: ProjectFile) => {
    try {
      const result = await researchApi.addItemToLibrary(projectId, item.id)
      window.alert(result.already ? '这篇论文已经在文献库里了。' : '已加入文献库。')
    } catch (libraryError) {
      setError(errorText(libraryError))
    }
  }

  const deleteSelected = async () => {
    if (selectedFiles.length === 0) return
    if (
      !window.confirm(
        `确定删除选中的 ${selectedFiles.length} 项？\n\n文件夹里的文件会一起删除，删除后无法恢复。`,
      )
    ) {
      return
    }
    let next = files
    try {
      for (const id of selectedFiles) {
        next = await researchApi.deleteItem(projectId, id)
      }
    } catch (deleteError) {
      setError(errorText(deleteError))
    } finally {
      applyItems(next)
      setSelectedFiles([])
    }
  }

  const copyPath = async (item: ProjectFile) => {
    // 普通文件/文件夹用后端解析出的绝对路径；虚拟节点在磁盘上没有独立目录时
    // 才退回相对路径，避免把「资料/a.pdf」这种片段写进剪贴板。
    const value = item.absolute || item.path || item.name
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value)
      } else {
        const area = document.createElement('textarea')
        area.value = value
        area.style.position = 'fixed'
        area.style.opacity = '0'
        document.body.appendChild(area)
        area.select()
        document.execCommand('copy')
        area.remove()
      }
    } catch {
      setError('复制路径失败，请检查剪贴板权限后重试。')
    }
  }

  const openSession = (session: SessionSummary) => {
    setPreview(null)
    setLocalPreview(null)
    setRailOpen(false)
    onSelectSession(session.id)
  }

  const startSessionRename = (session: SessionSummary) => {
    setSessionMenu(null)
    setSessionRenaming(session.id)
    setSessionDraft(session.title)
  }

  const commitSessionRename = async (session: SessionSummary) => {
    const title = sessionDraft.trim()
    setSessionRenaming(null)
    if (!title || title === session.title) return
    try {
      const updated = await researchApi.renameSession(session.id, title)
      setSessions((prev) =>
        prev.map((item) => (item.id === session.id ? updated : item)),
      )
      if (session.id === sessionId) setSessionTitle(updated.title)
    } catch (renameError) {
      setError(errorText(renameError))
    }
  }

  const deleteSession = async (session: SessionSummary) => {
    setSessionMenu(null)
    if (
      !window.confirm(
        `确定删除会话「${session.title}」？\n\n该会话的对话记录会被删除，无法恢复。`,
      )
    ) {
      return
    }
    try {
      await researchApi.deleteSession(session.id)
      setSessions((prev) => prev.filter((item) => item.id !== session.id))
      if (session.id === sessionId) onSelectSession(null)
    } catch (deleteError) {
      setError(errorText(deleteError))
    }
  }

  const copySessionValue = (value: string) => {
    void navigator.clipboard?.writeText(value)
  }

  const toggleSelected = (id: string) => {
    setSelectedFiles((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id],
    )
  }

  useEffect(() => {
    try {
      localStorage.setItem(RAIL_STORAGE_KEY, String(railWidth))
    } catch {
      /* 隐私模式等场景下只保留本次会话内的宽度 */
    }
  }, [railWidth])

  const beginRailResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    resizeStart.current = { x: event.clientX, width: railWidth }
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      /* 合成事件或指针已失效时仍靠 pointermove 更新 */
    }
    document.body.classList.add('is-resizing-rail')
  }

  const moveRailResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = resizeStart.current
    if (!start) return
    setRailWidth(clampRail(start.width + event.clientX - start.x))
  }

  const endRailResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!resizeStart.current) return
    resizeStart.current = null
    document.body.classList.remove('is-resizing-rail')
    try {
      event.currentTarget.releasePointerCapture(event.pointerId)
    } catch {
      /* 指针已释放 */
    }
  }

  const nudgeRail = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault()
      const delta = event.key === 'ArrowLeft' ? -16 : 16
      setRailWidth((width) => clampRail(width + delta))
    } else if (event.key === 'Home') {
      event.preventDefault()
      setRailWidth(RAIL_DEFAULT)
    }
  }

  return (
    <div
      className="project-page"
      style={{ '--project-rail-width': `${railWidth}px` } as CSSProperties}
    >
      <header className="project-head">
        <button className="project-back" onClick={back} title="返回">
          <IconBack />
          返回
        </button>

        {renaming ? (
          <input
            ref={renameRef}
            className="project-title-input"
            value={draftName}
            maxLength={80}
            onChange={(event) => setDraftName(event.target.value)}
            onBlur={() => void commitName()}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void commitName()
              if (event.key === 'Escape') {
                setDraftName(project?.name ?? '')
                setRenaming(false)
              }
            }}
          />
        ) : (
          <button
            className="project-title"
            onClick={() => setRenaming(true)}
            title="点击重命名"
          >
            {project?.name ?? '新项目'}
          </button>
        )}

        <button
          className="icon-btn project-rail-toggle"
          title="文件与会话"
          aria-label="文件与会话"
          aria-expanded={railOpen}
          onClick={() => setRailOpen((open) => !open)}
        >
          <IconFolder />
        </button>

        {selectedSession && (
          <div className="project-head-session">
            {selectedSession.running && <i className="project-running" />}
            <span
              className="project-head-title"
              title={sessionTitle ?? undefined}
            >
              {sessionTitle}
            </span>
          </div>
        )}

        <div className="project-head-actions">
          <button
            className="icon-btn"
            title="更多"
            onClick={() => setMenuOpen((open) => !open)}
          >
            ···
          </button>
          {menuOpen && (
            <div className="project-menu">
              <button onClick={() => void archive()}>归档项目</button>
              <button className="is-danger" onClick={() => void purge()}>
                删除项目
              </button>
            </div>
          )}
        </div>
      </header>

      {error && (
        <div className="project-error">
          <span>{error}</span>
          <button onClick={() => setError(null)}>
            <IconClose />
          </button>
        </div>
      )}

      <div className="project-body">
        {railOpen && (
          <button
            className="project-rail-scrim"
            aria-label="关闭文件与会话"
            onClick={() => setRailOpen(false)}
          />
        )}
        <aside className="project-rail" data-open={railOpen}>
          <div className="project-rail-head">
            <div className="project-rail-tabs" data-tour="project-rail-tabs">
              <button
                data-active={tab === 'files'}
                onClick={() => {
                  setTab('files')
                  void refreshArtifacts()
                }}
              >
                文件
              </button>
              <button
                data-active={tab === 'sessions'}
                onClick={() => setTab('sessions')}
              >
                会话
              </button>
            </div>
            <div className="project-rail-actions">
              {tab === 'sessions' && (
                <button
                  className="icon-btn"
                  title="新建会话"
                  onClick={openComposer}
                >
                  <IconPlus />
                </button>
              )}
            </div>
          </div>

          <input
            ref={uploadRef}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              void upload(event.target.files)
              event.target.value = ''
            }}
          />
          <input
            ref={folderRef}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              void uploadFolder(event.target.files)
              event.target.value = ''
            }}
          />

          {tab === 'sessions' ? (
            <>
              <div className="project-session-list">
              {sessions.length === 0 ? (
                <p className="project-rail-empty">还没有会话</p>
              ) : (
                sessions.map((session) => (
                  <div
                    key={session.id}
                    className="project-session-row"
                    role="button"
                    tabIndex={0}
                    data-active={session.id === sessionId}
                    onClick={() => openSession(session)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') openSession(session)
                    }}
                    onContextMenu={(event) => {
                      event.preventDefault()
                      setSessionMenu({
                        session,
                        x: event.clientX,
                        y: event.clientY,
                      })
                    }}
                  >
                    {sessionRenaming === session.id ? (
                      <input
                        autoFocus
                        className="project-session-input"
                        value={sessionDraft}
                        maxLength={80}
                        onClick={(event) => event.stopPropagation()}
                        onChange={(event) => setSessionDraft(event.target.value)}
                        onBlur={() => void commitSessionRename(session)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') {
                            event.preventDefault()
                            event.currentTarget.blur()
                          }
                          if (event.key === 'Escape') {
                            event.preventDefault()
                            setSessionRenaming(null)
                          }
                        }}
                      />
                    ) : (
                      <span className="project-session-name">{session.title}</span>
                    )}
                    <em>{relativeTime(session.updatedAt)}</em>
                    {session.running && <i className="project-running" />}
                    <button
                      type="button"
                      className="project-tree-more"
                      title="会话操作：重命名、复制、删除"
                      aria-label={`${session.title} 的会话操作`}
                      onClick={(event) => {
                        event.stopPropagation()
                        const rect = event.currentTarget.getBoundingClientRect()
                        setSessionMenu({
                          session,
                          x: rect.right - 4,
                          y: rect.bottom + 2,
                        })
                      }}
                    >
                      ⋯
                    </button>
                  </div>
                ))
              )}
            </div>
            {sessionMenu && (
              <div
                className="project-tree-menu"
                style={{
                  left: Math.min(
                    sessionMenu.x,
                    Math.max(8, window.innerWidth - 184),
                  ),
                  top: Math.min(
                    sessionMenu.y,
                    Math.max(8, window.innerHeight - 240),
                  ),
                }}
                onClick={(event) => event.stopPropagation()}
              >
                <button
                  type="button"
                  onClick={() => {
                    openSession(sessionMenu.session)
                    setSessionMenu(null)
                  }}
                >
                  <IconChat />
                  打开会话
                </button>
                <button
                  type="button"
                  onClick={() => startSessionRename(sessionMenu.session)}
                >
                  <IconEdit />
                  重命名
                </button>
                <div className="project-tree-menu-sep" />
                <button
                  type="button"
                  onClick={() => {
                    copySessionValue(sessionMenu.session.title)
                    setSessionMenu(null)
                  }}
                >
                  <IconCopy />
                  复制名称
                </button>
                <button
                  type="button"
                  onClick={() => {
                    copySessionValue(sessionMenu.session.id)
                    setSessionMenu(null)
                  }}
                >
                  <IconCopy />
                  复制会话 ID
                </button>
                <div className="project-tree-menu-sep" />
                <button
                  type="button"
                  className="is-danger"
                  onClick={() => void deleteSession(sessionMenu.session)}
                >
                  <IconTrash />
                  删除
                </button>
              </div>
            )}
          </>

          ) : (
            <div className="project-file-panel">
              {project?.rootPath ? (
                <ProjectExplorer
                  key={`${projectId}:${project.rootPath}`}
                  projectId={projectId}
                  rootPath={project.rootPath}
                  activeFile={localPreview?.entry.path ?? null}
                  onOpenFile={openLocalFile}

                  onError={setError}
                />
              ) : (
                <>
                  <div className="project-explorer-toolbar">
                    <button
                      className="project-explorer-choose"
                      title="选择电脑上的文件夹作为项目工作目录"
                      onClick={() => void pickWorkspaceFolder()}
                    >
                      <IconFolder />
                      打开本地文件夹
                    </button>
                  </div>
                  {folderParent !== undefined && (
                    <form
                      className="project-folder-form"
                      onSubmit={(event) => {
                        event.preventDefault()
                        void createFolder()
                      }}
                    >
                      <input
                        autoFocus
                        value={folderName}
                        placeholder="文件夹名称"
                        onChange={(event) => setFolderName(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Escape') setFolderParent(undefined)
                        }}
                      />
                      <button type="submit">确定</button>
                    </form>
                  )}
                  {uploading && <p className="project-rail-empty">正在上传…</p>}
                  <ProjectFileTree
                    items={treeItems}
                    activeFolderId={activeFolderId}
                    activeFileId={
                      preview?.id ??
                      (previewArtifact ? artifactNodeId(previewArtifact.id) : null)
                    }
                    selectedFiles={selectedFiles}
                    onFolder={(folder) => {
                      setSelectedFiles([])
                      if (folder.id === ARTIFACT_FOLDER_ID) return
                      setActiveFolderId(folder.id)
                    }}
                    onFile={(file) => {
                      setSelectedFiles([])
                      setRailOpen(false)
                      setLocalPreview(null)
                      if (file.kind === 'artifact') {
                        const artifact = artifacts.find(
                          (item) => item.id === file.refId,
                        )
                        if (!artifact) return
                        setPreview(null)
                        setPreviewArtifact(artifact)
                        return
                      }
                      setPreviewArtifact(null)
                      setPreview(file)
                    }}
                    onRefresh={() => void refreshArtifacts()}
                    onUpload={() => uploadRef.current?.click()}
                    onUploadFolder={() => folderRef.current?.click()}
                    onCreateFolder={startFolder}
                    onRename={(item, name) => renameFile(item, name)}
                    onDelete={(item) => void deleteFile(item)}
                    onMove={(item, parentId) => moveFile(item, parentId)}
                    onCopy={(item, parentId) => copyFile(item, parentId)}
                    onCopyPath={copyPath}
                    onAddToLibrary={(item) => void addToLibrary(item)}
                    onMultiSelect={toggleSelected}
                    onDeleteSelected={() => void deleteSelected()}
                  />
                </>
              )}
            </div>
          )}
        <div
          className="project-rail-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整文件与会话栏宽度"
          title="拖动调整宽度，双击复位"
          aria-valuemin={RAIL_MIN}
          aria-valuemax={RAIL_MAX}
          aria-valuenow={railWidth}
          tabIndex={0}
          onPointerDown={beginRailResize}
          onPointerMove={moveRailResize}
          onPointerUp={endRailResize}
          onPointerCancel={endRailResize}
          onDoubleClick={() => setRailWidth(RAIL_DEFAULT)}
          onKeyDown={nudgeRail}
        />
        </aside>

        <section className="project-main">
          {loading ? (
            <div className="project-loading">正在加载项目…</div>
          ) : previewArtifact ? (
            <ArtifactPreview
              projectId={projectId}
              artifact={previewArtifact}
              onClose={() => setPreviewArtifact(null)}
            />
          ) : preview ? (
            <FilePreview
              projectId={projectId}
              file={preview}
              onClose={() => setPreview(null)}
            />
          ) : localPreview ? (
            <LocalFilePreview
              url={localPreview.url}
              file={{
                name: localPreview.entry.name,
                path: localPreview.entry.path,
                size: localPreview.entry.size,
                mime: localPreview.entry.mime,
              }}
              onClose={() => setLocalPreview(null)}
            />
          ) : selectedSession ? (
            <SessionView
              key={selectedSession.id}
              session={selectedSession}
              initialText={pendingText}
              onInitialTextConsumed={() => {
                setPendingText(null)
                onInitialTextConsumed?.()
              }}
              onMeta={updateSessionMeta}
              onTitle={setSessionTitle}
              kind={project?.kind ?? 'research'}
              onOpenSettings={() => setSettingsOpen(true)}
              workspaceName={workspaceLabel || undefined}
              onPickWorkspace={() => void pickWorkspaceFolder()}
              onFilesUploaded={(uploaded) =>
                setFiles((prev) => [...prev, ...uploaded])
              }
            />
          ) : (
            <div className="project-empty">
              <IconChat />
              <h2>在项目中发起会话</h2>
              <p>
                {project?.kind === 'review'
                  ? '从综述主题、文献或大纲开始。'
                  : '描述研究问题，或先在左侧上传一份文献。'}
              </p>
              <Composer
                autoFocus
                running={false}
                kind={project?.kind ?? 'research'}
                projectId={projectId}
                placeholder="Enter 发送 · Shift+Enter 换行"
                hint={sending ? '正在创建会话…' : '会话将在发送后创建'}
                onUploaded={(file) => setFiles((prev) => [...prev, file])}
                onSend={(text) => void send(text)}
                onStop={() => undefined}
                onOpenSettings={() => setSettingsOpen(true)}
                workspaceName={workspaceLabel || undefined}
                onPickWorkspace={() => void pickWorkspaceFolder()}
              />
            </div>
          )}
        </section>
      </div>

      <LocalFolderBrowser
        open={folderPicker}
        initialPath={project?.rootPath || ''}
        onClose={() => setFolderPicker(false)}
        onSelect={applyRoot}
      />
      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
    </div>
  )
}

/** 产物预览：研究 agent 生成的清单 / 方案 / 综述 / 汇报提纲。 */
function ArtifactPreview({
  projectId,
  artifact,
  onClose,
}: {
  projectId: string
  artifact: ArtifactSummary
  onClose: () => void
}) {
  const url = researchApi.artifactContentUrl(projectId, artifact.id)
  const [text, setText] = useState('')
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    setFailed(false)
    fetch(url)
      .then((response) => {
        if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`)
        return response.text()
      })
      .then((value) => {
        if (alive) setText(value)
      })
      .catch(() => {
        if (alive) setFailed(true)
      })
    return () => {
      alive = false
    }
  }, [url])

  return (
    <div className="project-preview">
      <header className="project-preview-head">
        <div className="project-preview-heading">
          <FileTypeIcon name={nameWithSuffix(artifact.title, artifact.path)} />
          <div>
            <h2>{nameWithSuffix(artifact.title, artifact.path)}</h2>
            <span>
              {artifact.kind}
              {artifact.path ? ` · ${artifact.path}` : ''}
            </span>
          </div>
        </div>
        <div className="project-preview-actions">
          <a
            className="icon-btn"
            href={url}
            download={artifact.path || artifact.title}
            title="下载"
          >
            <IconDownload />
          </a>
          <button className="icon-btn" title="关闭预览" onClick={onClose}>
            <IconClose />
          </button>
        </div>
      </header>
      <div className="project-preview-body">
        {failed ? (
          <p className="project-preview-note">产物文件已不存在，可能被移动或删除。</p>
        ) : (
          <div className="project-preview-markdown">
            <Markdown text={text} />
          </div>
        )}
      </div>
    </div>
  )
}
