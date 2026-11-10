/**
 * 服务器可达性探测。
 *
 * 手机离开电脑后仍然可能连着网（能直连大模型），但电脑上的 Paperead 后端
 * 已经不可达。阅读、问答、翻译都要据此切换到本机引擎。
 */
import { apiUrl, isMobileShell } from './mobile'

const CACHE_MS = 15_000

let checkedAt = 0
let lastOk = false
let inflight: Promise<boolean> | null = null

// 网络切换后立刻作废旧结论，避免刚离开电脑时仍按“在线”走服务器
if (typeof window !== 'undefined') {
  window.addEventListener('offline', () => {
    checkedAt = 0
    lastOk = false
  })
  window.addEventListener('online', () => {
    checkedAt = 0
  })
}

/** 浏览器明确报告断网时直接返回 true，省一次失败请求。 */
export function networkOffline(): boolean {
  return typeof navigator !== 'undefined' && navigator.onLine === false
}

/**
 * 探测 `/api/health`。结果缓存 15 秒，避免阅读时每个块都发一次请求。
 * `force` 用于用户主动重试或从后台切回来。
 */
export async function serverReachable(force = false): Promise<boolean> {
  if (networkOffline()) {
    lastOk = false
    checkedAt = Date.now()
    return false
  }
  if (!force && Date.now() - checkedAt < CACHE_MS) return lastOk
  if (inflight) return inflight

  inflight = (async () => {
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), 2500)
    try {
      const response = await fetch(apiUrl('/api/health'), {
        signal: controller.signal,
        cache: 'no-store',
      })
      lastOk = response.ok
    } catch {
      lastOk = false
    } finally {
      window.clearTimeout(timer)
      checkedAt = Date.now()
      inflight = null
    }
    return lastOk
  })()

  return inflight
}

/** 本机引擎可用条件：在手机壳里，并且浏览器报告有网。 */
export function canRunLocally(): boolean {
  if (networkOffline()) return false
  return isMobileShell() && typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}
