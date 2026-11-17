import { browser } from 'wxt/browser'
import type { PaperMetadata, PaperSummary } from './types'

const PORTS = Array.from({ length: 10 }, (_, index) => 17321 + index)
let baseCache = ''

export async function discoverBase(force = false): Promise<string> {
  if (baseCache && !force) return baseCache

  const saved = await browser.storage.local.get('base')
  if (!force && typeof saved.base === 'string' && saved.base) {
    baseCache = saved.base
    return baseCache
  }

  for (const port of PORTS) {
    const base = `http://127.0.0.1:${port}`
    try {
      const response = await fetch(`${base}/api/ext/v1/ping`, {
        signal: AbortSignal.timeout(300),
      })
      if (!response.ok) continue
      const data = (await response.json()) as { app?: string }
      if (data.app !== 'paperead') continue
      baseCache = base
      await browser.storage.local.set({ base })
      return base
    } catch {
      // 该端口没有 Paperead，继续探测下一个
    }
  }
  throw new Error('Paperead 未启动，请先打开桌面端')
}

async function token(): Promise<string> {
  const saved = await browser.storage.local.get('pluginToken')
  return typeof saved.pluginToken === 'string' ? saved.pluginToken : ''
}

export async function isPaired(): Promise<boolean> {
  return Boolean(await token())
}

/**
 * 配对。code 为空时走「等待认领」：桌面端点过「连接浏览器插件」后，
 * 不带码也能直接绑定，用户不需要抄数字。
 */
export async function pair(code = ''): Promise<void> {
  const base = await discoverBase()
  let response: Response
  try {
    response = await fetch(`${base}/api/ext/v1/pair`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, name: 'Paperead Connector' }),
    })
  } catch {
    throw new Error('无法连接 Paperead，请确认桌面端已启动并重启到最新版本')
  }
  const data = (await response.json().catch(() => ({}))) as { detail?: string; token?: string }
  if (!response.ok || !data.token) {
    throw new Error(data.detail || `配对失败（HTTP ${response.status}）`)
  }
  await browser.storage.local.set({ pluginToken: data.token })
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const send = async (): Promise<Response> => {
    const base = await discoverBase()
    const headers = new Headers(init.headers)
    headers.set('Authorization', `Bearer ${await token()}`)
    return fetch(`${base}${path}`, { ...init, headers })
  }

  let response: Response
  try {
    response = await send()
  } catch {
    // 桌面端可能换了端口或重启过，清掉缓存再探一次
    baseCache = ''
    await browser.storage.local.remove('base')
    try {
      response = await send()
    } catch {
      throw new Error('无法连接 Paperead，请确认桌面端已启动并重启到最新版本')
    }
  }
  const data = (await response.json().catch(() => ({}))) as { detail?: string }
  if (response.status === 401) {
    await browser.storage.local.remove('pluginToken')
    throw new Error('插件未连接或授权已撤销，请重新配对')
  }
  if (!response.ok) {
    throw new Error(data.detail || `请求失败（HTTP ${response.status}）`)
  }
  return data as T
}

export async function lookup(meta: PaperMetadata): Promise<{
  found: boolean
  paper?: PaperSummary
}> {
  const params = new URLSearchParams()
  if (meta.source_id) params.set('source_id', meta.source_id)
  if (meta.doi) params.set('doi', meta.doi)
  if (meta.url) params.set('url', meta.url)
  return request(`/api/ext/v1/papers/lookup?${params.toString()}`)
}

export async function ingest(
  bytes: Uint8Array,
  meta: PaperMetadata,
  folderId = '',
): Promise<{ paper: PaperSummary; already: boolean }> {
  const form = new FormData()
  const blob = new Blob([bytes as unknown as BlobPart], { type: 'application/pdf' })
  form.append('file', new File([blob], 'paper.pdf', { type: 'application/pdf' }))
  form.append('metadata', JSON.stringify(meta))
  if (folderId) form.append('folder_id', folderId)
  return request('/api/ext/v1/papers/ingest', { method: 'POST', body: form })
}

export async function getPaper(paperId: string): Promise<PaperSummary> {
  const data = await request<{ paper: PaperSummary; open_url: string }>(
    `/api/ext/v1/papers/${encodeURIComponent(paperId)}`,
  )
  return { ...data.paper, open_url: data.open_url }
}

export async function listFolders(): Promise<
  Array<{ id: string; name: string; count: number }>
> {
  const data = await request<{
    folders: Array<{ id: string; name: string; count: number }>
  }>('/api/ext/v1/folders')
  return data.folders
}

/** 本地是否已配置可用模型；不返回密钥。 */
export async function readingStatus(): Promise<{
  model_ready: boolean
  model_label: string
}> {
  return request('/api/ext/v1/reading/status')
}

/** 划词翻译：使用桌面端设置里选中的模型，扩展不持有 API key。 */
export async function translateSelection(
  text: string,
  targetLang = 'zh-Hans',
): Promise<{ ok: boolean; translation: string; target_name: string }> {
  return request('/api/ext/v1/reading/translate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, target_lang: targetLang }),
  })
}

/** 针对已解析论文的选中段落提问。 */
export async function askSelection(
  paperId: string,
  question: string,
): Promise<{ answer: string; sources?: unknown[] }> {
  return request('/api/ext/v1/reading/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paper_id: paperId, question, web: false }),
  })
}

export interface AskStreamHandlers {
  onStatus?: (message: string) => void
  onSources?: (sources: unknown[]) => void
  onDelta?: (text: string) => void
  onDone?: () => void
  onError?: (message: string) => void
}

/** 旧服务端没有流式接口，调用方据此回退到 askSelection。 */
export class AskStreamUnsupportedError extends Error {
  constructor() {
    super('streaming not supported')
    this.name = 'AskStreamUnsupportedError'
  }
}

function frameData(frame: string): string {
  const lines: string[] = []
  for (const line of frame.split(/\r?\n/)) {
    if (!line.startsWith('data:')) continue
    lines.push(line.slice(5).replace(/^ /, ''))
  }
  return lines.join('\n')
}

/** 返回 true 表示收到 done，可以停止读取。 */
function dispatchStreamEvent(raw: string, handlers: AskStreamHandlers): boolean {
  if (!raw.trim()) return false
  let payload: unknown
  try {
    payload = JSON.parse(raw)
  } catch {
    return false
  }
  if (!payload || typeof payload !== 'object') return false
  const data = payload as Record<string, unknown>
  const type = String(data.type || '')
  if (type === 'status') {
    handlers.onStatus?.(String(data.message ?? data.text ?? data.detail ?? ''))
  } else if (type === 'sources') {
    handlers.onSources?.(Array.isArray(data.sources) ? data.sources : [])
  } else if (type === 'delta') {
    handlers.onDelta?.(String(data.text ?? data.delta ?? ''))
  } else if (type === 'done') {
    handlers.onDone?.()
    return true
  } else if (type === 'error') {
    handlers.onError?.(String(data.detail ?? data.message ?? '生成失败'))
  }
  return false
}

/**
 * 流式问答。按 SSE 的 \n\n 分帧，支持 data: 多行与 AbortController。
 * 旧服务端返回 404/405 时抛出 AskStreamUnsupportedError。
 */
export async function askSelectionStream(
  paperId: string,
  question: string,
  handlers: AskStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const send = async (): Promise<Response> => {
    const base = await discoverBase()
    return fetch(`${base}/api/ext/v1/reading/ask/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${await token()}`,
      },
      body: JSON.stringify({ paper_id: paperId, question, web: false }),
      signal,
    })
  }

  let response: Response
  try {
    response = await send()
  } catch (error) {
    if (signal?.aborted) throw error
    baseCache = ''
    await browser.storage.local.remove('base')
    try {
      response = await send()
    } catch {
      throw new Error('无法连接 Paperead，请确认桌面端已启动并重启到最新版本')
    }
  }

  if (response.status === 404 || response.status === 405) {
    throw new AskStreamUnsupportedError()
  }
  if (response.status === 401) {
    await browser.storage.local.remove('pluginToken')
    throw new Error('插件未连接或授权已撤销，请重新配对')
  }
  if (!response.ok) {
    const data = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(data.detail || `请求失败（HTTP ${response.status}）`)
  }
  if (!response.body) throw new AskStreamUnsupportedError()

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finished = false

  try {
    while (!finished) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let match = /\r?\n\r?\n/.exec(buffer)
      while (match) {
        const raw = frameData(buffer.slice(0, match.index))
        buffer = buffer.slice(match.index + match[0].length)
        if (dispatchStreamEvent(raw, handlers)) {
          finished = true
          break
        }
        match = /\r?\n\r?\n/.exec(buffer)
      }
    }
    if (!finished) {
      buffer += decoder.decode()
      const raw = frameData(buffer)
      if (raw) dispatchStreamEvent(raw, handlers)
    }
  } finally {
    if (finished) await reader.cancel().catch(() => {})
  }
}

// ── 阅读助手：导读 / 阅读重点 / 批注 ─────────────────────

export interface ReadingGuide {
  status?: string
  summary?: string
  oneliner?: string
  domains?: string[]
  difficulty?: { stars?: number; minutes?: number; math?: string; terms?: string }
  overview?: {
    problem?: string
    approach?: string
    contributions?: string[]
    findings?: string[]
    limitations?: string[]
  }
  glossary?: Array<{
    en?: string
    zh?: string
    kind?: string
    explain?: string
    where?: string
  }>
  outline?: Array<{ section?: string; what?: string; focus?: string }>
  questions?: string[]
  path?: Array<{
    kind?: string
    title?: string
    year?: number | null
    concept?: string
    minutes?: number
    page?: number
    section?: string
    why?: string
    order?: string
    gain?: string
  }>
  prereqs?: Array<{
    concept?: string
    en?: string
    why?: string
    paper?: { title?: string; year?: number | null }
  }>
  degraded?: boolean
  [key: string]: unknown
}

export interface ReadingHighlight {
  category?: string
  kind?: string
  text?: string
  quote?: string
  quote_en?: string
  quote_zh?: string
  content?: string
  page?: number
  [key: string]: unknown
}

export type ReadingHighlightGroups =
  | ReadingHighlight[]
  | Record<string, Array<ReadingHighlight | string>>

export interface ReadingHighlights {
  highlights?: ReadingHighlightGroups | null
  pages?: number[]
  categories?: Array<string | { key?: string; label?: string }>
  translated_pages?: number | number[]
  needs_translation?: boolean
}

export interface ReadingAnnotation {
  id?: string
  quote?: string
  note?: string
  page?: number
  block_id?: string
  created_at?: number
  [key: string]: unknown
}

/** 已缓存的导读；guide 为 null 表示还没生成。 */
export async function getReadingGuide(
  paperId: string,
): Promise<{ status: string; guide: ReadingGuide | null }> {
  const data = await request<{ status?: string; guide?: ReadingGuide | null }>(
    `/api/ext/v1/reading/guide?paper_id=${encodeURIComponent(paperId)}`,
  )
  const guide = data.guide ?? null
  return { status: data.status || (guide ? 'ready' : 'missing'), guide }
}

/** 生成导读。服务端可能直接返回 guide，也可能包在 { guide } 里。 */
export async function makeReadingGuide(paperId: string): Promise<ReadingGuide | null> {
  const data = await request<{ guide?: ReadingGuide | null } | ReadingGuide>(
    '/api/ext/v1/reading/guide',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paper_id: paperId }),
    },
  )
  if (data && typeof data === 'object' && 'guide' in data) {
    return (data as { guide?: ReadingGuide | null }).guide ?? null
  }
  return data as ReadingGuide
}

export async function getReadingHighlights(
  paperId: string,
): Promise<ReadingHighlights> {
  return request(
    `/api/ext/v1/reading/highlights?paper_id=${encodeURIComponent(paperId)}`,
  )
}

/** 生成阅读重点；needs_translation 为真时要先在阅读页生成译文。 */
export async function makeReadingHighlights(
  paperId: string,
  pages: number[] = [],
  force = false,
): Promise<ReadingHighlights> {
  return request('/api/ext/v1/reading/highlights', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paper_id: paperId, pages, force }),
  })
}

export async function getReadingAnnotations(
  paperId: string,
): Promise<ReadingAnnotation[]> {
  const data = await request<{ annotations?: ReadingAnnotation[] }>(
    `/api/ext/v1/reading/annotations?paper_id=${encodeURIComponent(paperId)}`,
  )
  return data.annotations ?? []
}

export async function createReadingAnnotation(
  paperId: string,
  quote: string,
  note: string,
  blockId = '',
  page = 0,
): Promise<ReadingAnnotation | null> {
  const data = await request<
    { annotation?: ReadingAnnotation | null } | ReadingAnnotation
  >('/api/ext/v1/reading/annotations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      paper_id: paperId,
      quote,
      note,
      block_id: blockId,
      page,
    }),
  })
  if (data && typeof data === 'object' && 'annotation' in data) {
    return (data as { annotation?: ReadingAnnotation | null }).annotation ?? null
  }
  return data as ReadingAnnotation
}
