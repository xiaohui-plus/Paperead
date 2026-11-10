/**
 * 安卓壳专用：App 内置前端，通过 HTTP 连接独立部署的 Paperead 后端。
 *
 * 桌面版由 Tauri 直接把窗口指向本机 sidecar，同源访问，不会进入这里；
 * 安卓版页面来源是 WebView 本地源，所有 API / 媒体请求都要补上服务器地址，
 * 并用 token 头（媒体用查询参数）通过服务端鉴权。
 */

const SERVER_KEY = 'paperead.mobile.server'
const TOKEN_KEY = 'paperead.mobile.token'

export interface MobileServer {
  base: string
  token: string
}

/** 安卓 WebView（Tauri）或显式 `?edition=mobile` 时进入移动版。 */
export function isMobileShell(): boolean {
  if (typeof window === 'undefined') return false
  if (new URLSearchParams(window.location.search).get('edition') === 'mobile') {
    return true
  }
  const tauri = Boolean((window as any).__TAURI_INTERNALS__?.invoke)
  return tauri && /Android/i.test(navigator.userAgent)
}

/** 补全协议、去掉末尾斜杠；非法输入抛错。 */
export function normalizeServerBase(input: string): string {
  let value = input.trim()
  if (!value) throw new Error('请填写服务器地址')
  if (!/^https?:\/\//i.test(value)) value = `http://${value}`
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    throw new Error('服务器地址格式不正确')
  }
  if (!parsed.hostname) throw new Error('服务器地址格式不正确')
  return `${parsed.protocol}//${parsed.host}`.replace(/\/+$/, '')
}

export function loadMobileServer(): MobileServer {
  try {
    const base = localStorage.getItem(SERVER_KEY) || ''
    const token = localStorage.getItem(TOKEN_KEY) || ''
    return { base, token }
  } catch {
    return { base: '', token: '' }
  }
}

export function saveMobileServer(base: string, token: string): MobileServer {
  const normalized = normalizeServerBase(base)
  localStorage.setItem(SERVER_KEY, normalized)
  localStorage.setItem(TOKEN_KEY, token.trim())
  // 换了服务器要重新拉一次模型配置，否则会继续用旧电脑的 Key
  try {
    sessionStorage.removeItem('paperead.models.pulled')
  } catch {
    // sessionStorage 不可用时忽略
  }
  return { base: normalized, token: token.trim() }
}

export function clearMobileServer(): void {
  localStorage.removeItem(SERVER_KEY)
  localStorage.removeItem(TOKEN_KEY)
}

export function hasMobileServer(): boolean {
  return Boolean(loadMobileServer().base)
}

/** 把 `/api/...` 拼成当前服务器的绝对地址。 */
export function apiUrl(path: string): string {
  const { base } = loadMobileServer()
  if (!base) return path
  return `${base}${path.startsWith('/') ? path : `/${path}`}`
}

/**
 * 直连服务器的 fetch：自动补 token 头。
 *
 * `installMobileFetch` 只改写以 `/api` 开头的相对路径；用 `apiUrl()` 拼成绝对
 * 地址的请求会绕过它，所以这些地方必须走 apiFetch，否则桌面临时口令校验会 403。
 */
export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const { base, token } = loadMobileServer()
  const url = base ? `${base}${path.startsWith('/') ? path : `/${path}`}` : path
  const headers = new Headers(init?.headers || {})
  if (token && !headers.has('X-Paperead-Token')) {
    headers.set('X-Paperead-Token', token)
  }
  return fetch(url, { ...init, headers })
}

/**
 * 媒体地址（图片 / PDF / 缩略图）：`<img src>`、pdf.js 都带不了自定义头，
 * 所以把 token 放进查询参数，由服务端 guard 放行。
 */
export function mediaUrl(path: string | null | undefined): string {
  if (!path) return ''
  if (/^(https?:|blob:|data:)/i.test(path)) return path
  // 只改写后端资源；/demo/* 等前端内置文件保持原样
  if (!path.startsWith('/api')) return path
  const { base, token } = loadMobileServer()
  if (!base) return path
  const url = `${base}${path.startsWith('/') ? path : `/${path}`}`
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}pr_token=${encodeURIComponent(token)}`
}

let fetchInstalled = false

/**
 * 全局包装 fetch：只在移动壳里生效，把相对 `/api` 请求转到服务器并附 token。
 * 这样不需要改动几十处调用点，桌面版行为完全不变。
 *
 * 扫码配对后可以再次调用：只安装一次，避免重复包裹导致请求头叠加。
 */
export function installMobileFetch(): void {
  if (fetchInstalled) return
  if (!isMobileShell() || !hasMobileServer()) return
  fetchInstalled = true
  const original = window.fetch.bind(window)
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    if (typeof input === 'string' && input.startsWith('/api')) {
      const { token } = loadMobileServer()
      const headers = new Headers(init?.headers || {})
      if (token && !headers.has('X-Paperead-Token')) {
        headers.set('X-Paperead-Token', token)
      }
      return original(apiUrl(input), { ...init, headers })
    }
    return original(input as RequestInfo, init)
  }
}

/** 首次配置时测试服务器是否可达、token 是否正确。 */
export async function testMobileServer(base: string, token: string): Promise<void> {
  const normalized = normalizeServerBase(base)
  const headers: Record<string, string> = {}
  if (token.trim()) headers['X-Paperead-Token'] = token.trim()
  let res: Response
  try {
    res = await fetch(`${normalized}/api/health`, { headers })
  } catch {
    throw new Error('无法连接服务器，请检查地址和网络')
  }
  if (res.status === 403) throw new Error('访问口令不正确')
  if (!res.ok) throw new Error(`服务器返回 HTTP ${res.status}`)
  const data = (await res.json().catch(() => ({}))) as { ok?: boolean; version?: string }
  if (data.ok === false) throw new Error('服务器健康检查失败')
}
