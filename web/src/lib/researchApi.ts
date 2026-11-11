/** 研究项目 / 文件树 / 会话索引 API 客户端。 */

import type { AgentEvent } from './researchTypes'

export type ProjectKind = 'research' | 'review'

export interface ProjectSummary {
  id: string
  kind: ProjectKind
  name: string
  description: string
  archived: boolean
  /** 用户选择的本地工作文件夹；为空表示用项目自己的文件夹。 */
  rootPath: string
  createdAt: number
  updatedAt: number
  sessionCount: number
  running: boolean
}

export interface FsEntry {
  name: string
  /** 本机浏览时是绝对路径，项目浏览时是相对路径。 */
  path: string
  absolute?: string
  isDir: boolean
  size: number
  modifiedAt: number
  kind: 'folder' | 'file'
  mime?: string
}

export interface FsListing {
  path: string
  parent: string | null
  items: FsEntry[]
}

export interface ProjectListing {
  root: string
  path: string
  parent: string
  items: FsEntry[]
}

export interface ProjectFile {
  id: string
  parentId: string | null
  kind: 'folder' | 'file' | 'paper' | 'note' | 'artifact'
  name: string
  path: string
  mime: string
  size: number
  refId: string | null
  meta: Record<string, unknown>
  addedAt: number
}

export interface ArtifactSummary {
  id: string
  projectId: string
  sessionId: string | null
  kind: string
  title: string
  path: string
  version: number
  createdAt: number
  meta: Record<string, unknown>
}

export interface SessionSummary {
  id: string
  projectId: string
  title: string
  status: string
  model: string
  createdAt: number
  updatedAt: number
  lastTurnAt: number | null
  running: boolean
}

export type ReviewTemplateSource = 'default' | 'builtin' | 'upload' | 'paper'

export interface ReviewTemplateFigure {
  label: string
  caption: string
  page: number
  section: string
  type?: string
  purpose?: string
  context?: string
}

export interface ReviewTemplateSectionStat {
  title: string
  page: number
  pages: number
  chars: number
  figures: number
  tables: number
  images?: number
}

export interface ReviewTemplateStyleRow {
  term: string
  count: number
  per10k: number
}

export interface ReviewTemplateStyle {
  citationStyle: string
  sentenceUnit: string
  avgSentenceLength: number
  longSentenceRatio: number
  avgParagraphLength: number
  firstPersonPer10k: number
  connectives: ReviewTemplateStyleRow[]
  hedges: ReviewTemplateStyleRow[]
}

export interface ReviewTemplateFormat {
  pageSize?: string
  columns?: number
  bodyFontSize?: number
  titleFontSize?: number
  marginsMm?: Record<string, number>
  headingStyle?: string
  headingExample?: string
  captionSeparator?: string
  abstract?: boolean
  keywords?: boolean
  referenceHeading?: string
  citationStyle?: string
}

export interface ReviewTemplateGuide {
  paperId?: string
  title?: string
  sections?: string[]
  figures?: ReviewTemplateFigure[]
  tables?: ReviewTemplateFigure[]
  sectionStats?: ReviewTemplateSectionStat[]
  style?: ReviewTemplateStyle
  format?: ReviewTemplateFormat
  notes?: string[]
  markdown?: string
}

export type ReviewTemplateKind = 'paper' | 'review' | ''

export interface ReviewTemplateOutlineItem {
  title: string
  required?: boolean
  words?: string
  points: string[]
  checks: string[]
}

export interface ReviewTemplateOption {
  id: string
  name: string
  source: ReviewTemplateSource
  description: string
  sections: string[]
  kind?: ReviewTemplateKind
  outline?: ReviewTemplateOutlineItem[]
  guide?: ReviewTemplateGuide
}

export interface ReviewTemplateDetail extends ReviewTemplateOption {
  markdown: string
  error: string
}

export interface ReviewTemplatePaper {
  id: string
  title: string
  authors: string
  year: string | number
  pages: number
  status: string
  isReview: boolean
}

export interface ReviewTemplateState {
  selected: string
  templates: ReviewTemplateOption[]
  selectedDetail: ReviewTemplateDetail
  library: ReviewTemplatePaper[]
  uploaded?: ReviewTemplateOption
}

export type ResearchMode = 'standard' | 'deep'
export type WorkspaceScope = 'workspace' | 'readonly'

export interface SendMeta {
  mode: ResearchMode
  scope: WorkspaceScope
}

export interface ResearchCommand {
  name: string
  description: string
  input?: { hint: string }
}

export interface MessageRef {
  kind: 'file' | 'paper'
  name: string
  path?: string
  paperId?: string
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(
      (data as { detail?: string }).detail || `请求失败（HTTP ${res.status}）`,
    )
  }
  return data as T
}

function json(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export const researchApi = {
  async listProjects(kind: ProjectKind): Promise<ProjectSummary[]> {
    const data = await jsonFetch<{ items: ProjectSummary[] }>(
      `/api/research/projects?kind=${kind}`,
    )
    return data.items
  },

  createProject(
    kind: ProjectKind,
    name?: string,
    rootPath?: string,
  ): Promise<ProjectSummary> {
    return jsonFetch<ProjectSummary>(
      '/api/research/projects',
      json('POST', { kind, name: name ?? '', rootPath: rootPath ?? '' }),
    )
  },

  setProjectRoot(projectId: string, path: string): Promise<ProjectSummary> {
    return jsonFetch<ProjectSummary>(
      `/api/research/projects/${projectId}/root`,
      json('PATCH', { path }),
    )
  },

  browseFs(path = ''): Promise<FsListing> {
    return jsonFetch<FsListing>(
      `/api/research/fs/list?path=${encodeURIComponent(path)}`,
    )
  },

  async fsRoots(): Promise<FsEntry[]> {
    const data = await jsonFetch<{ items: FsEntry[] }>('/api/research/fs/roots')
    return data.items
  },

  projectExplorer(projectId: string, path = ''): Promise<ProjectListing> {
    return jsonFetch<ProjectListing>(
      `/api/research/projects/${projectId}/explorer?path=${encodeURIComponent(path)}`,
    )
  },

  projectExplorerFileUrl(projectId: string, path: string): string {
    return `/api/research/projects/${projectId}/explorer/file?path=${encodeURIComponent(path)}`
  },

  getProject(projectId: string): Promise<ProjectSummary> {
    return jsonFetch<ProjectSummary>(`/api/research/projects/${projectId}`)
  },

  renameProject(projectId: string, name: string): Promise<ProjectSummary> {
    return jsonFetch<ProjectSummary>(
      `/api/research/projects/${projectId}`,
      json('PATCH', { name }),
    )
  },

  archiveProject(projectId: string): Promise<ProjectSummary> {
    return jsonFetch<ProjectSummary>(`/api/research/projects/${projectId}`, {
      method: 'DELETE',
    })
  },

  /** 永久删除项目及其全部文件、会话和产物。 */
  async purgeProject(projectId: string): Promise<void> {
    await jsonFetch(`/api/research/projects/${projectId}/purge`, {
      method: 'DELETE',
    })
  },

  async listFiles(projectId: string): Promise<ProjectFile[]> {
    const data = await jsonFetch<{ items: ProjectFile[] }>(
      `/api/research/projects/${projectId}/files`,
    )
    return data.items
  },

  createFolder(
    projectId: string,
    name: string,
    parentId: string | null,
  ): Promise<ProjectFile> {
    return jsonFetch<ProjectFile>(
      `/api/research/projects/${projectId}/folders`,
      json('POST', { name, parentId }),
    )
  },

  async uploadFile(
    projectId: string,
    file: File,
    parentId: string | null,
  ): Promise<ProjectFile> {
    const form = new FormData()
    form.append('file', file)
    if (parentId) form.append('parentId', parentId)
    return jsonFetch<ProjectFile>(`/api/research/projects/${projectId}/files`, {
      method: 'POST',
      body: form,
    })
  },

  /**
   * 导入用户自己的文件夹：paths 与 entries 一一对应，
   * 后端会按 `/` 拆分并保留子目录结构。
   */
  async uploadFolder(
    projectId: string,
    entries: { file: File; path: string }[],
    parentId: string | null,
  ): Promise<{ items: ProjectFile[]; added: number; errors: string[] }> {
    const form = new FormData()
    entries.forEach((entry) => form.append('files', entry.file))
    form.append('paths', JSON.stringify(entries.map((entry) => entry.path)))
    if (parentId) form.append('parentId', parentId)
    return jsonFetch(`/api/research/projects/${projectId}/folder`, {
      method: 'POST',
      body: form,
    })
  },

  fileContentUrl(projectId: string, itemId: string): string {
    return `/api/research/projects/${projectId}/files/${itemId}/content`
  },

  async renameItem(
    projectId: string,
    itemId: string,
    name: string,
  ): Promise<ProjectFile[]> {
    const data = await jsonFetch<{ items: ProjectFile[] }>(
      `/api/research/projects/${projectId}/items/${itemId}`,
      json('PATCH', { name }),
    )
    return data.items
  },

  async moveItem(
    projectId: string,
    itemId: string,
    parentId: string | null,
  ): Promise<ProjectFile[]> {
    const data = await jsonFetch<{ items: ProjectFile[] }>(
      `/api/research/projects/${projectId}/items/${itemId}`,
      json('PATCH', { parentId }),
    )
    return data.items
  },

  async copyItem(
    projectId: string,
    itemId: string,
    parentId: string | null,
  ): Promise<ProjectFile[]> {
    const data = await jsonFetch<{ items: ProjectFile[] }>(
      `/api/research/projects/${projectId}/items/${itemId}/copy`,
      json('POST', { parentId }),
    )
    return data.items
  },

  async deleteItem(projectId: string, itemId: string): Promise<ProjectFile[]> {
    const data = await jsonFetch<{ items: ProjectFile[] }>(
      `/api/research/projects/${projectId}/items/${itemId}`,
      { method: 'DELETE' },
    )
    return data.items
  },

  addItemToLibrary(
    projectId: string,
    itemId: string,
  ): Promise<{ ok: boolean; already: boolean; paperId: string }> {
    return jsonFetch<{ ok: boolean; already: boolean; paperId: string }>(
      `/api/research/projects/${projectId}/items/${itemId}/to-library`,
      { method: 'POST' },
    )
  },

  async listArtifacts(projectId: string): Promise<ArtifactSummary[]> {
    const data = await jsonFetch<{ items: ArtifactSummary[] }>(
      `/api/research/projects/${projectId}/artifacts`,
    )
    return data.items
  },

  artifactContentUrl(projectId: string, artifactId: string): string {
    return `/api/research/projects/${projectId}/artifacts/${artifactId}/content`
  },

  async listSessions(projectId: string): Promise<SessionSummary[]> {
    const data = await jsonFetch<{ items: SessionSummary[] }>(
      `/api/research/projects/${projectId}/sessions`,
    )
    return data.items
  },

  createSession(projectId: string, title?: string): Promise<SessionSummary> {
    return jsonFetch<SessionSummary>(
      `/api/research/projects/${projectId}/sessions`,
      json('POST', { title: title ?? '' }),
    )
  },

  async sendMessage(
    sessionId: string,
    text: string,
    meta?: SendMeta,
  ): Promise<void> {
    await jsonFetch<{ ok: boolean }>(
      `/api/research/sessions/${sessionId}/messages`,
      json('POST', {
        text,
        mode: meta?.mode ?? 'standard',
        scope: meta?.scope ?? 'workspace',
      }),
    )
  },

  async listCommands(sessionId: string): Promise<ResearchCommand[]> {
    const data = await jsonFetch<{ items: ResearchCommand[] }>(
      `/api/research/sessions/${sessionId}/commands`,
    )
    return data.items
  },

  runCommand(
    sessionId: string,
    line: string,
  ): Promise<{ ok: boolean; event: AgentEvent }> {
    return jsonFetch<{ ok: boolean; event: AgentEvent }>(
      `/api/research/sessions/${sessionId}/commands`,
      json('POST', { line }),
    )
  },

  async cancelSession(sessionId: string): Promise<void> {
    await jsonFetch<{ ok: boolean }>(
      `/api/research/sessions/${sessionId}/cancel`,
      { method: 'POST' },
    )
  },

  getReviewTemplate(): Promise<ReviewTemplateState> {
    return jsonFetch<ReviewTemplateState>('/api/research/review-templates')
  },

  selectReviewTemplate(id: string): Promise<ReviewTemplateState> {
    return jsonFetch<ReviewTemplateState>(
      '/api/research/review-templates/select',
      json('POST', { id }),
    )
  },

  async uploadReviewTemplate(file: File): Promise<ReviewTemplateState> {
    const form = new FormData()
    form.append('file', file)
    return jsonFetch<ReviewTemplateState>('/api/research/review-templates/upload', {
      method: 'POST',
      body: form,
    })
  },

  deleteReviewTemplateUpload(name: string): Promise<ReviewTemplateState> {
    return jsonFetch<ReviewTemplateState>(
      `/api/research/review-templates/uploads/${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    )
  },

  previewReviewTemplatePaper(
    paperId: string,
    refresh = false,
  ): Promise<ReviewTemplateOption & { error: string }> {
    const suffix = refresh ? '?refresh=true' : ''
    return jsonFetch<ReviewTemplateOption & { error: string }>(
      `/api/research/review-templates/paper/${encodeURIComponent(paperId)}${suffix}`,
    )
  },
}

export function relativeTime(seconds: number | null | undefined): string {
  if (!seconds) return ''
  const delta = Math.max(0, Math.floor(Date.now() / 1000) - seconds)
  if (delta < 60) return '刚刚'
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`
  if (delta < 86400 * 2) return '昨天'
  if (delta < 86400 * 30) return `${Math.floor(delta / 86400)} 天前`
  const date = new Date(seconds * 1000)
  return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`
}

export function formatSize(bytes: number): string {
  if (!bytes) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
