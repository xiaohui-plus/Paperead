/** 文献检索 API 客户端。后端在 `/api/scholar/*`，错误统一转成中文消息。 */

import type { PaperRow } from './library'

export type ScholarSource =
  | 'arxiv'
  | 'pubmed'
  | 'openalex'
  | 'europepmc'
  | 'semanticscholar'
  | 'core'

export interface ScholarPaper {
  id: string
  source: ScholarSource
  title: string
  authors: string[]
  venue: string
  year: number | null
  category: string
  keywords: string[]
  abstract: string
  url: string
  pdf_url: string | null
  html_url: string | null
  published_at: string | null
  doi: string | null
  citations: number | null
}

export interface SearchParams {
  q: string
  source: ScholarSource
  sort: 'rel' | 'new' | 'updated'
  page: number
  field: 'all' | 'title' | 'abstract' | 'author'
  phrase: boolean
  category: string
  fromYear: number | null
  toYear: number | null
  perPage: number
}

export interface SearchResponse {
  papers: ScholarPaper[]
  total: number
  page_limit: number | null
  used_query: string
  translated: boolean
  error: string | null
}

export interface DailyResponse {
  papers: ScholarPaper[]
  date: string
  pool_size: number
  error: string | null
}

export interface FullTextResponse {
  pdf_url: string | null
  landing_url: string | null
  oa_status: string | null
  error: string | null
}

export const SOURCE_META: { k: ScholarSource; l: string; d: string }[] = [
  { k: 'arxiv', l: 'arXiv', d: '物理、数学、计算机等预印本' },
  { k: 'pubmed', l: 'PubMed', d: '生物医学与生命科学文献' },
  { k: 'openalex', l: 'OpenAlex', d: '全学科开放学术图谱，附带被引量' },
  { k: 'europepmc', l: 'Europe PMC', d: '生物医学全文、预印本与被引量' },
  {
    k: 'semanticscholar',
    l: 'Semantic Scholar',
    d: '全学科检索，自带开放获取 PDF 与引用量；建议配置 PAPEREAD_S2_API_KEY 避免限流',
  },
  { k: 'core', l: 'CORE', d: '开放获取全文聚合，需配置 API Key' },
]

export const SOURCE_LABELS: Record<ScholarSource, string> = {
  arxiv: 'arXiv',
  pubmed: 'PubMed',
  openalex: 'OpenAlex',
  europepmc: 'Europe PMC',
  semanticscholar: 'Semantic Scholar',
  core: 'CORE',
}

export const SORT_META = [
  { k: 'rel', l: '相关度' },
  { k: 'new', l: '最新提交' },
  { k: 'updated', l: '最新更新' },
] as const

export const FIELD_META = [
  { k: 'all', l: '全部字段' },
  { k: 'title', l: '标题' },
  { k: 'abstract', l: '摘要' },
  { k: 'author', l: '作者' },
] as const

export const PER_PAGE = [10, 20, 30, 50] as const

export const CITATION_STYLES = [
  { k: 'gb7714', l: 'GB/T 7714' },
  { k: 'apa', l: 'APA' },
  { k: 'mla', l: 'MLA' },
  { k: 'ieee', l: 'IEEE' },
  { k: 'bibtex', l: 'BibTeX' },
  { k: 'ris', l: 'RIS' },
] as const

export type CitationStyle = (typeof CITATION_STYLES)[number]['k']

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((data as { detail?: string }).detail || `请求失败（HTTP ${res.status}）`)
  return data as T
}

function postJson<T>(url: string, body: unknown): Promise<T> {
  return jsonFetch<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function searchPapers(params: SearchParams): Promise<SearchResponse> {
  const query = new URLSearchParams({
    q: params.q,
    source: params.source,
    sort: params.sort,
    page: String(params.page),
    field: params.field,
    phrase: params.phrase ? 'true' : 'false',
    per_page: String(params.perPage),
  })
  if (params.category.trim()) query.set('category', params.category.trim())
  if (params.fromYear) query.set('from_year', String(params.fromYear))
  if (params.toYear) query.set('to_year', String(params.toYear))
  return jsonFetch<SearchResponse>(`/api/scholar/search?${query}`)
}

export function getScholarSources(): Promise<{ disabled: Partial<Record<ScholarSource, string>> }> {
  return jsonFetch<{ disabled: Partial<Record<ScholarSource, string>> }>('/api/scholar/sources')
}

export function getDaily(round: number): Promise<DailyResponse> {
  return jsonFetch<DailyResponse>(`/api/scholar/daily?round=${round}`)
}

export function getScholarPaper(id: string): Promise<ScholarPaper> {
  return jsonFetch<ScholarPaper>(`/api/scholar/paper?id=${encodeURIComponent(id)}`)
}

export function findFreeFullText(doi: string): Promise<FullTextResponse> {
  return jsonFetch<FullTextResponse>(`/api/scholar/fulltext?doi=${encodeURIComponent(doi)}`)
}

export function importScholarPaper(id: string): Promise<{ paper: PaperRow; imported: boolean; already: boolean }> {
  return postJson(`/api/scholar/import`, { id })
}

export async function citeScholarPaper(paper: ScholarPaper, style: CitationStyle): Promise<string> {
  const data = await postJson<{ text: string }>('/api/scholar/cite', { paper, style })
  return data.text
}

export function exportScholarPapers(
  papers: ScholarPaper[],
  format: 'bibtex' | 'ris',
): Promise<{ text: string; filename: string }> {
  return postJson('/api/scholar/export', { papers, format })
}

/** 触发浏览器下载纯文本文件。 */
export function downloadText(filename: string, text: string, mime = 'text/plain;charset=utf-8'): void {
  const blob = new Blob([text], { type: mime })
  const href = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = href
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(href)
}

/** 把检索词拆成可高亮的片段（长度 > 1 的词，避免把 a/the 全标黄）。 */
export function highlightTerms(query: string): string[] {
  return query
    .trim()
    .split(/\s+/)
    .filter((t) => t.length > 1)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
}

export function truncateText(text: string, limit: number): string {
  const value = (text || '').trim()
  return value.length <= limit ? value : `${value.slice(0, limit - 1).trimEnd()}…`
}
