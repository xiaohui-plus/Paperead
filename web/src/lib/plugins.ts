import { apiFetch } from './mobile'

export interface PluginInfo {
  id: string
  name: string
  origin: string
  scopes: string[]
  created_at: number
  last_used_at: number
}

export interface PairingCode {
  code: string
  expires_at: number
}

async function readJson<T>(response: Response): Promise<T> {
  const data = (await response.json().catch(() => ({}))) as {
    detail?: string
  }
  if (!response.ok) {
    throw new Error(data.detail || `请求失败（HTTP ${response.status}）`)
  }
  return data as T
}

export async function createPairingCode(name = '浏览器扩展'): Promise<PairingCode> {
  const response = await apiFetch('/api/ext/v1/pair/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  return readJson<PairingCode>(response)
}

/** 开一个等待窗口：扩展在 120 秒内不带码调用 /pair 即可自动绑定。 */
export async function startPairingWait(): Promise<{ expires_at: number }> {
  const response = await apiFetch('/api/ext/v1/pair/wait', { method: 'POST' })
  return readJson<{ expires_at: number }>(response)
}

export async function listPlugins(): Promise<PluginInfo[]> {
  const response = await apiFetch('/api/ext/v1/admin/plugins')
  const data = await readJson<{ plugins: PluginInfo[] }>(response)
  return data.plugins
}

export async function revokePlugin(id: string): Promise<void> {
  const response = await apiFetch(
    `/api/ext/v1/admin/plugins/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
  )
  await readJson<{ ok: boolean }>(response)
}
