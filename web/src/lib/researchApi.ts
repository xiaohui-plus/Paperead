/** 研究项目 / 文件树 / 会话索引 API 客户端。 */

export type ProjectKind = 'research' | 'review'

export interface ProjectSummary {
  id: string
  kind: ProjectKind
  name: string
  description: string
  archived: boolean
  createdAt: number
  updatedAt: number
  sessionCount: number
  running: boolean
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

  createProject(kind: ProjectKind, name?: string): Promise<ProjectSummary> {
    return jsonFetch<ProjectSummary>(
      '/api/research/projects',
      json('POST', { kind, name: name ?? '' }),
    )
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

  async sendMessage(sessionId: string, text: string): Promise<void> {
    await jsonFetch<{ ok: boolean }>(
      `/api/research/sessions/${sessionId}/messages`,
      json('POST', { text }),
    )
  },

  async cancelSession(sessionId: string): Promise<void> {
    await jsonFetch<{ ok: boolean }>(
      `/api/research/sessions/${sessionId}/cancel`,
      { method: 'POST' },
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
