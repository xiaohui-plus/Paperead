import { useCallback, useEffect, useRef, useState } from 'react'
import {
  IconBack,
  IconChat,
  IconClose,
  IconDownload,
  IconPlus,
} from '../components/icons'
import { Markdown } from '../components/Markdown'
import {
  relativeTime,
  researchApi,
  type ArtifactSummary,
  type ProjectFile,
  type ProjectKind,
  type ProjectSummary,
  type SessionSummary,
} from '../lib/researchApi'
import { Composer } from './Composer'
import { ProjectFileTree } from './ProjectFileTree'
import { FilePreview } from './preview/FilePreview'
import { SessionView } from './SessionView'

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
  const [project, setProject] = useState<ProjectSummary | null>(null)
  const [files, setFiles] = useState<ProjectFile[]>([])
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([])
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [tab, setTab] = useState<Tab>('sessions')
  const [preview, setPreview] = useState<ProjectFile | null>(null)
  const [previewArtifact, setPreviewArtifact] = useState<ArtifactSummary | null>(null)
  const [activeFolderId, setActiveFolderId] = useState<string | null>(null)
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [folderParent, setFolderParent] = useState<string | null | undefined>(undefined)
  const [folderName, setFolderName] = useState('')
  const [uploading, setUploading] = useState(false)

  const [renaming, setRenaming] = useState(false)
  const [draftName, setDraftName] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  const [pendingText, setPendingText] = useState<string | null>(
    initialText ?? null,
  )
  const [sending, setSending] = useState(false)

  const renameRef = useRef<HTMLInputElement>(null)
  const uploadRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let alive = true
    setLoading(true)
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
        setArtifacts(loadedArtifacts)
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

  useEffect(() => {
    if (!renaming) return
    const input = renameRef.current
    if (input) {
      input.focus()
      input.select()
    }
  }, [renaming])

  // 同一项目内再次从首页发起时，props 会给出新的首条消息。
  useEffect(() => {
    if (initialText) setPendingText(initialText)
  }, [initialText])

  // agent 写产物不会触发前端刷新；切回「文件」时重新拉一次。
  const refreshArtifacts = useCallback(async () => {
    try {
      const [nextFiles, nextArtifacts] = await Promise.all([
        researchApi.listFiles(projectId),
        researchApi.listArtifacts(projectId),
      ])
      setFiles(nextFiles)
      setArtifacts(nextArtifacts)
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
      onBack(project.kind)
    } catch (archiveError) {
      setError(errorText(archiveError))
    }
  }

  const openComposer = () => {
    setPreview(null)
    onSelectSession(null)
    setTab('sessions')
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
      applyItems(await researchApi.moveItem(projectId, item.id, parentId))
    } catch (moveError) {
      setError(errorText(moveError))
    }
  }

  const copyFile = async (item: ProjectFile, parentId: string | null) => {
    try {
      applyItems(await researchApi.copyItem(projectId, item.id, parentId))
    } catch (copyError) {
      setError(errorText(copyError))
    }
  }

  const deleteFile = async (item: ProjectFile) => {
    if (!window.confirm(`确定删除「${item.name}」？`)) return
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
    if (!window.confirm(`确定删除选中的 ${selectedFiles.length} 项？`)) return
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

  const copyPath = (item: ProjectFile) => {
    const value = item.path || item.name
    void navigator.clipboard?.writeText(value)
  }

  const toggleSelected = (id: string) => {
    setSelectedFiles((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id],
    )
  }

  return (
    <div className="project-page">
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
        <aside className="project-rail">
          <div className="project-rail-head">
            <div className="project-rail-tabs">
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

          {tab === 'sessions' ? (
            <div className="project-session-list">
              {sessions.length === 0 ? (
                <p className="project-rail-empty">还没有会话</p>
              ) : (
                sessions.map((session) => (
                  <button
                    key={session.id}
                    className="project-session-row"
                    data-active={session.id === sessionId}
                    onClick={() => {
                      setPreview(null)
                      onSelectSession(session.id)
                    }}
                  >
                    <span className="project-session-name">{session.title}</span>
                    <em>{relativeTime(session.updatedAt)}</em>
                    {session.running && <i className="project-running" />}
                  </button>
                ))
              )}
            </div>
          ) : (
            <div className="project-file-panel">
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
                onCreateFolder={startFolder}
                onRename={(item, name) => void renameFile(item, name)}
                onDelete={(item) => void deleteFile(item)}
                onMove={(item, parentId) => void moveFile(item, parentId)}
                onCopy={(item, parentId) => void copyFile(item, parentId)}
                onCopyPath={copyPath}
                onAddToLibrary={(item) => void addToLibrary(item)}
                onMultiSelect={toggleSelected}
                onDeleteSelected={() => void deleteSelected()}
              />
            </div>
          )}
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
                placeholder="Enter 发送 · Shift+Enter 换行"
                hint={sending ? '正在创建会话…' : '会话将在发送后创建'}
                onSend={(text) => void send(text)}
                onStop={() => undefined}
              />
            </div>
          )}
        </section>
      </div>
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
        <div>
          <h2>{nameWithSuffix(artifact.title, artifact.path)}</h2>
          <span>
            {artifact.kind}
            {artifact.path ? ` · ${artifact.path}` : ''}
          </span>
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
