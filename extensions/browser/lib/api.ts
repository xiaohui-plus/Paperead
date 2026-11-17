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
