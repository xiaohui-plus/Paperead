/** 文献库 API 客户端。后端没启动时自动回落到内置示例。 */

export type PaperStatus = 'uploaded' | 'parsing' | 'ready' | 'failed'

export interface PaperRow {
  id: string
  title: string
  authors: string
  filename: string
  pages: number
  size: number
  created_at: number
  status: PaperStatus
  error: string
  translated: number
  blocks: number
  thumb_url: string | null
  file_url: string
}

export interface LLMConfigPayload {
  base_url: string
  api_key: string
  model: string
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((data as any).detail || `HTTP ${res.status}`)
  return data as T
}

export async function listPapers(): Promise<PaperRow[]> {
  const data = await jsonFetch<{ papers: PaperRow[] }>('/api/papers')
  return data.papers
}

export async function uploadPapers(
  files: File[],
): Promise<{ added: PaperRow[]; failed: { filename: string; error: string }[] }> {
  const fd = new FormData()
  files.forEach((f) => fd.append('files', f))
  return jsonFetch('/api/papers/upload', { method: 'POST', body: fd })
}

export async function deletePaper(id: string): Promise<void> {
  await jsonFetch(`/api/papers/${id}`, { method: 'DELETE' })
}

export async function reparsePaper(id: string): Promise<void> {
  await jsonFetch(`/api/papers/${id}/parse`, { method: 'POST' })
}

export async function translatePaper(id: string, config: LLMConfigPayload): Promise<void> {
  await jsonFetch(`/api/papers/${id}/translate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config }),
  })
}

/** 逐条读取 SSE `data:` 事件；翻译与问答共用。 */
async function readSSE<T>(res: Response, onEvent: (event: T) => void): Promise<void> {
  const reader = res.body?.getReader()
  if (!reader) throw new Error('无法读取响应流')

  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let sep = buffer.indexOf('\n\n')
    while (sep >= 0) {
      const chunk = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue
        try {
          onEvent(JSON.parse(line.slice(5).trim()) as T)
        } catch {
          // 半行/坏行直接忽略
        }
      }
      sep = buffer.indexOf('\n\n')
    }
  }
}

/**
 * 按页流式翻译：服务端每翻完一段推一条 `block` 事件，前端逐段渲染。
 * 首段通常 2–3 秒出现，不用等整页翻完。
 */
export interface TranslateEvent {
  type: 'start' | 'block' | 'done' | 'error'
  block?: Record<string, unknown>
  page?: number
  total?: number
  translated?: number
  pages?: number[] | null
  message?: string
}

export async function translateStream(
  id: string,
  config: LLMConfigPayload,
  pages: number[],
  onEvent: (e: TranslateEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api/papers/${id}/translate/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config, pages }),
    signal,
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error((data as any).detail || `HTTP ${res.status}`)
  }
  await readSSE<TranslateEvent>(res, onEvent)
}

export async function probeZotero(): Promise<{ ok: boolean; error?: string }> {
  return jsonFetch('/api/zotero/probe')
}

// ── 导读 ────────────────────────────────────────────────

export interface GuidePaper {
  index: number
  title: string
  year: number | null
  raw: string
}

export interface GuidePrereq {
  concept: string
  en: string
  why: string
  paper: GuidePaper
}

export interface GuideStep {
  kind: 'ref' | 'paper'
  title: string
  year?: number | null
  minutes: number
  gain: string
}

export interface Guide {
  status: string
  summary: string
  domains: string[]
  difficulty: { stars?: number; minutes?: number; math?: string; terms?: string }
  prereqs: GuidePrereq[]
  path: GuideStep[]
  concepts?: { name: string; en: string; kind: string }[]
  degraded?: boolean
}

export async function getGuide(id: string): Promise<Guide | null> {
  const res = await fetch(`/api/papers/${id}/guide`)
  if (res.status === 404) return null
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((data as any).detail || `HTTP ${res.status}`)
  return data as Guide
}

export async function makeGuide(
  id: string,
  config: LLMConfigPayload,
  refresh = false,
): Promise<Guide> {
  return jsonFetch(`/api/papers/${id}/guide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config, refresh }),
  })
}

// ── 问答 ────────────────────────────────────────────────

export interface AskSource {
  type: 'paper' | 'ref' | 'web'
  label: string
  page?: number
  blockId?: string
  snippet?: string
  index?: number
  title?: string
  url?: string
}

export interface AskResult {
  answer: string
  sources: AskSource[]
  usedWeb: boolean
  latency: number
}

export async function askPaper(
  id: string,
  config: LLMConfigPayload,
  question: string,
  web = true,
): Promise<AskResult> {
  return jsonFetch(`/api/papers/${id}/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config, question, web }),
  })
}

export interface AskStreamEvent {
  type: 'status' | 'sources' | 'delta' | 'done' | 'error'
  stage?: string
  sources?: AskSource[]
  usedWeb?: boolean
  text?: string
  latency?: number
  message?: string
}

/**
 * 流式问答：`status`（检索中）→ `sources`（出处）→ `delta`×N → `done` / `error`。
 * 出错时已经收到的增量仍然有效，调用方保留即可。
 */
export async function askPaperStream(
  id: string,
  config: LLMConfigPayload,
  question: string,
  web: boolean,
  onEvent: (event: AskStreamEvent) => void,
  clientId?: string,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api/papers/${id}/ask/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config, question, web, ...(clientId ? { client_id: clientId } : {}) }),
    signal,
  })
  if (res.status === 404) {
    // 兼容旧服务 / 旧桌面 sidecar：没有流式端点时退回一次性接口，
    // 前端仍然按 Markdown 渲染，只是不再逐字出现。
    const legacy = await askPaper(id, config, question, web)
    onEvent({ type: 'sources', sources: legacy.sources, usedWeb: legacy.usedWeb })
    if (legacy.answer) onEvent({ type: 'delta', text: legacy.answer })
    onEvent({ type: 'done', latency: legacy.latency })
    return
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error((data as any).detail || `HTTP ${res.status}`)
  }
  await readSSE<AskStreamEvent>(res, onEvent)
}

/** 后端按「一轮问答」给出的历史记录，用于刷新后恢复对话。 */
export interface ChatTurn {
  id: string
  question: string
  answer: string
  sources: AskSource[]
  usedWeb: boolean
  latency?: number
  at?: number
}

/** 分页读取历史对话：`before` 传上一页第一轮的 id，往前翻。 */
export async function getChat(
  id: string,
  before?: string,
  limit = 50,
): Promise<{ turns: ChatTurn[]; hasMore: boolean }> {
  const qs = new URLSearchParams({ limit: String(limit) })
  if (before) qs.set('before', before)
  return jsonFetch<{ turns: ChatTurn[]; hasMore: boolean }>(
    `/api/papers/${id}/chat?${qs.toString()}`,
  )
}

// ── 思路来源图 ──────────────────────────────────────────

export interface LineageNode {
  id: number
  title: string
  year: number | null
  role: string
  roleLabel: string
  borrow: string
  improve: string
  evidence: string
  cites: number
  marker: string
}

export interface Lineage {
  self: { title: string; year: number | null }
  nodes: LineageNode[]
  edges: { from: number; to: string; relation: string; weight: number }[]
  yearRange: [number, number]
}

export async function getLineage(id: string): Promise<Lineage | null> {
  const res = await fetch(`/api/papers/${id}/lineage`)
  if (res.status === 404) return null
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((data as any).detail || `HTTP ${res.status}`)
  return data as Lineage
}

export async function makeLineage(
  id: string,
  config: LLMConfigPayload,
  refresh = false,
): Promise<Lineage> {
  return jsonFetch(`/api/papers/${id}/lineage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config, refresh }),
  })
}

// ── 追本溯源 ────────────────────────────────────────────

export interface ProvenanceCitation {
  marker: string
  snippet: string
  url: string
  source: string
  ref: { index: number; title: string; year: number | null; raw: string }
}

export async function getProvenance(
  paperId: string,
  blockId: string,
): Promise<ProvenanceCitation[]> {
  const data = await jsonFetch<{ citations: ProvenanceCitation[] }>(
    `/api/papers/${paperId}/block/${blockId}/provenance`,
  )
  return data.citations ?? []
}

export async function importZotero(
  limit = 20,
): Promise<{ imported: number; skipped: number; errors: string[] }> {
  return jsonFetch('/api/zotero/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ limit }),
  })
}

/** 后端不可用时的示例条目。 */
export const DEMO_PAPER: PaperRow = {
  id: 'demo',
  title: 'Structural Information Enhanced Graph Representation for Link Prediction',
  authors: 'Lei Shi, Bin Hu, Deng Zhao, Jianshan He, Zhiqiang Zhang, Jun Zhou',
  filename: 'demo.pdf',
  pages: 9,
  size: 778408,
  created_at: Date.now() / 1000,
  status: 'ready',
  error: '',
  translated: 71,
  blocks: 160,
  thumb_url: '/demo/thumb.png',
  file_url: '/demo/paper.json',
}
