/** 外部论文笔记（本地导入）API 客户端。 */

import { isDesktopApp } from './update'

export interface NoteRow {
  id: string
  title: string
  conference: string
  field: string
  tags: string[]
  description: string
  source_path: string
  license?: string
  origin?: string
  imported_at?: number
}

export interface NoteBlock {
  id: string
  kind: string
  level?: number
  text: string
  section?: string
  translatable?: boolean
}

export interface NoteDetail extends NoteRow {
  blocks: NoteBlock[]
  sections: Record<string, string>
  /** 已关联的文献库论文 id；为空表示还没导入 PDF */
  paper_id?: string
}

export interface ImportedPaper {
  id: string
  title: string
  status?: string
  blocks?: number
}

export interface NoteHit {
  id: string
  title: string
  conference: string
  field: string
  tags: string[]
  snippet: string
  score: number
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail = (data as { detail?: string }).detail
    if (detail && detail !== 'Not Found') throw new Error(detail)
    if (res.status === 404) throw new Error('接口不存在，后端可能还是旧版本，请重启服务')
    throw new Error(detail || `HTTP ${res.status}`)
  }
  return data as T
}

export async function listNotes(): Promise<NoteRow[]> {
  const data = await jsonFetch<{ notes: NoteRow[] }>('/api/notes')
  return data.notes
}

export async function searchNotes(query: string): Promise<NoteHit[]> {
  const params = new URLSearchParams({ q: query })
  const data = await jsonFetch<{ results: NoteHit[] }>(`/api/notes/search?${params}`)
  return data.results
}

export async function getNote(id: string): Promise<NoteDetail> {
  return jsonFetch<NoteDetail>(`/api/notes/${id}`)
}

export async function importNotes(
  path: string,
  limit = 0,
): Promise<{ imported: number; total: number }> {
  return jsonFetch('/api/notes/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, limit }),
  })
}

/** 桌面壳用系统目录选择器；浏览器拿不到绝对路径，返回 null 由调用方保留手填。 */
export async function pickNotesFolder(): Promise<string | null> {
  if (!isDesktopApp()) return null
  const { open } = await import('@tauri-apps/plugin-dialog')
  const picked = await open({
    directory: true,
    multiple: false,
    title: '选择 paper-notes 的 docs 目录',
  })
  return typeof picked === 'string' ? picked : null
}

/** 从笔记里的论文链接下载 PDF 并入库，成功后笔记会带上 paper_id。 */
export async function importNotePaper(
  noteId: string,
): Promise<{ paper: ImportedPaper; created: boolean; cached: boolean; url?: string }> {
  return jsonFetch(`/api/notes/${noteId}/import-paper`, { method: 'POST' })
}

/** 把笔记块拼回 markdown：正文本身就是 markdown 原文，只补标题层级。 */
export function blocksToMarkdown(blocks: NoteBlock[]): string {
  return blocks
    .map((block) => {
      if (block.kind === 'title') return `# ${block.text}`
      if (block.kind === 'heading') {
        const level = Math.min(Math.max(block.level || 2, 2), 6)
        return `${'#'.repeat(level)} ${block.text}`
      }
      return block.text
    })
    .join('\n\n')
}
export interface CatalogItem {
  path: string
  conference: string
  field: string
  label: string
  slug: string
  title: string
}

export interface CatalogFieldItem {
  name: string
  label: string
  count: number
}

export interface CatalogGroup {
  name: string
  count: number
  fields: CatalogFieldItem[]
}

export interface CatalogCard {
  name: string
  count: number
  fieldCount: number
  groups: CatalogGroup[]
}

export interface CatalogOverview {
  status: {
    state: 'idle' | 'loading' | 'ready' | 'error'
    total: number
    stale: boolean
    error: string
  }
  total: number
  conferenceCount: number
  fieldCount: number
  groups: CatalogGroup[]
  conferences: CatalogCard[]
}

export interface CatalogConference {
  name: string
  count: number
}

export interface CatalogField {
  conference: string
  name: string
  count: number
}

export interface CatalogResult {
  state: 'idle' | 'loading' | 'ready' | 'error'
  total: number
  matched: number
  offset: number
  limit: number
  updated_at: number
  stale: boolean
  error: string
  items: CatalogItem[]
  conferences: CatalogConference[]
  fields: CatalogField[]
}

export interface FieldNote extends CatalogItem {
  description: string
}

export interface FieldTheme {
  word: string
  count: number
}

export interface FieldResult {
  items: FieldNote[]
  matched: number
  offset: number
  limit: number
  across: { name: string; count: number }[]
  themes: FieldTheme[]
}

/** 按需补摘要：一次最多 30 条，返回 path → description。 */
export async function fetchDescriptions(
  paths: string[],
): Promise<Record<string, string>> {
  if (!paths.length) return {}
  const params = new URLSearchParams({ paths: paths.join(",") })
  const data = await jsonFetch<{ descriptions: Record<string, string> }>(
    `/api/notes/catalog/descriptions?${params}`,
  )
  return data.descriptions
}

/** 领域页：当前页论文带摘要，并附跨会议统计与高频主题。 */
export async function fetchFieldNotes(
  conference: string,
  field: string,
  limit = 30,
  offset = 0,
): Promise<FieldResult> {
  const params = new URLSearchParams({
    conference,
    field,
    limit: String(limit),
    offset: String(offset),
  })
  return jsonFetch<FieldResult>(`/api/notes/catalog/field?${params}`)
}

/** 笔记广场目录：只取路径与标题，正文在点开时按需加载。 */
export async function fetchOverview(): Promise<CatalogOverview> {
  return jsonFetch<CatalogOverview>('/api/notes/catalog/overview')
}

export async function fetchCatalog(params: {
  conference?: string
  field?: string
  q?: string
  limit?: number
  offset?: number
}): Promise<CatalogResult> {
  const search = new URLSearchParams()
  if (params.conference) search.set('conference', params.conference)
  if (params.field) search.set('field', params.field)
  if (params.q) search.set('q', params.q)
  search.set('limit', String(params.limit ?? 60))
  search.set('offset', String(params.offset ?? 0))
  return jsonFetch<CatalogResult>(`/api/notes/catalog?${search}`)
}

export async function refreshCatalog(): Promise<CatalogResult> {
  return jsonFetch('/api/notes/catalog/refresh', { method: 'POST' })
}

/** 按仓库内路径在线读取单篇笔记；后端会顺带写入本地缓存。 */
export async function fetchOnlineNote(
  path: string,
): Promise<{ note: NoteDetail; cached: boolean }> {
  const params = new URLSearchParams({ path })
  return jsonFetch(`/api/notes/online?${params}`)
}