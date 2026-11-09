/** 版本更新检查：一天最多自动检查一次，手动检查不受限制。 */

export interface UpdateInfo {
  current: string
  latest: string
  has_update: boolean
  notes: string
  pub_date: string
  url: string
  release_url: string
  source?: string
  error?: string
  cached?: boolean
  checked_at?: number
}

interface UpdateCache {
  checkedAt: number
  data: UpdateInfo
}

const CACHE_KEY = 'paperead.update.check'
export const UPDATE_DAY_MS = 24 * 60 * 60 * 1000

export function loadUpdateCache(): UpdateCache | null {
  try {
    const raw = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null')
    if (!raw || !raw.checkedAt || !raw.data) return null
    return raw as UpdateCache
  } catch {
    return null
  }
}

/** force=false 时后端可能直接返回当天缓存；force=true 会重新拉取更新清单。 */
export async function checkUpdate(force = false): Promise<UpdateInfo> {
  const res = await fetch(`/api/update${force ? '?force=1' : ''}`)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
  if (data.error) throw new Error(String(data.error))
  const info = data as UpdateInfo
  const checkedAt = Number(info.checked_at) || Date.now()
  const cached: UpdateCache = { checkedAt, data: info }
  localStorage.setItem(CACHE_KEY, JSON.stringify(cached))
  return { ...info, checked_at: checkedAt }
}

export function updateIsStale(cache: UpdateCache | null = loadUpdateCache()): boolean {
  return !cache || Date.now() - cache.checkedAt >= UPDATE_DAY_MS
}

/** 应用启动时调用：一天内只自动请求一次，失败静默。 */
export async function autoCheckUpdate(): Promise<UpdateInfo | null> {
  if (!updateIsStale()) return null
  try {
    return await checkUpdate(false)
  } catch {
    return null
  }
}

export function formatCheckTime(ts: number): string {
  if (!ts) return '尚未检查'
  try {
    return new Date(ts).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return '刚刚'
  }
}

interface TauriInternals {
  invoke?: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>
}

/** 是否运行在桌面客户端里（只有桌面版才能直接安装更新）。 */
export function canInstallInApp(): boolean {
  if (typeof window === 'undefined') return false
  return Boolean((window as any).__TAURI_INTERNALS__?.invoke)
}

/**
 * 交给 Tauri 更新器：重新检查、下载并启动安装程序。
 * 安装开始后应用会退出，所以成功时通常等不到返回值。
 */
export async function installUpdateInApp(): Promise<string> {
  const internals = (window as any).__TAURI_INTERNALS__ as TauriInternals | undefined
  if (!internals?.invoke) {
    throw new Error('当前环境不支持自动安装，请下载安装包')
  }
  const version = await internals.invoke('install_update')
  return String(version ?? '')
}

