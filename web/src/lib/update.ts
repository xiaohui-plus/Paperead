/** 版本更新检查：一天最多自动检查一次，手动检查不受限制。 */

import webPackage from '../../package.json'

export { mergeInstallProgress } from './updateProgress'

/**
 * 前端构建自带的版本号。
 * 网页端（没有 Tauri、后端也没起）时用它兜底，避免设置页显示上次检查缓存里的旧版本。
 */
export const BUNDLED_VERSION =
  typeof webPackage?.version === 'string' ? webPackage.version : ''

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
  /** Android 安装包信息（桌面端为空） */
  fallback_urls?: string[]
  sha256?: string
  size?: number
}

/** Android 更新清单：固定从 Gitee 的 latest 发行版检查；GitHub 只作手动下载备份。 */
export const ANDROID_UPDATE_ENDPOINTS = [
  'https://gitee.com/deng_xiaohui/paperead/releases/download/latest/android.json',
]

const ANDROID_RELEASE_PAGE = 'https://gitee.com/deng_xiaohui/paperead/releases'

interface UpdateCache {
  checkedAt: number
  data: UpdateInfo
}

const CACHE_KEY = 'paperead.update.check'
export const UPDATE_DAY_MS = 24 * 60 * 60 * 1000
/** 打开设置页时，缓存超过这个时间就静默复查一次，避免发布新版本后仍显示旧结果。 */
export const UPDATE_RECHECK_MS = 5 * 60 * 1000

/** 桌面更新器上报的阶段进度。 */
export interface UpdateProgress {
  stage:
    | 'checking'
    | 'downloading'
    /** 后台已把安装包下载完，只差点击安装 */
    | 'ready'
    | 'installing'
    | 'launching'
    | 'up-to-date'
    | 'failed'
  version?: string
  downloaded?: number
  total?: number
  percent?: number
  message?: string
  /**
   * 事件来源：manual（设置页主动更新）、menu（帮助菜单）、background（后台预下载）。
   * 设置页只消费 manual，避免后台下载的进度把正在显示的进度条拉回去。
   */
  source?: 'manual' | 'menu' | 'background' | string
}

const UPDATE_PROGRESS_EVENT = 'paperead:update-progress'

/** 旧版后端返回秒级 checked_at，统一换算成毫秒，避免显示成 1970 年。 */
export function normalizeCheckedAt(value: unknown): number {
  const n = Number(value) || 0
  return n > 0 && n < 1e12 ? Math.round(n * 1000) : n
}

/** 版本号比较：仅当 latest 严格高于 current 时才算可更新。 */
export function isNewerVersion(latest: string, current: string): boolean {
  if (!latest) return false
  if (!current) return true
  const key = (value: string) =>
    value
      .replace(/^v/i, '')
      .split(/[.\-+]/)
      .map((part) => Number.parseInt(part, 10) || 0)
  const next = key(latest)
  const base = key(current)
  const length = Math.max(next.length, base.length)
  for (let i = 0; i < length; i += 1) {
    const a = next[i] ?? 0
    const b = base[i] ?? 0
    if (a !== b) return a > b
  }
  return false
}

export function loadUpdateCache(): UpdateCache | null {
  try {
    const raw = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null')
    if (!raw || !raw.checkedAt || !raw.data) return null
    return { checkedAt: normalizeCheckedAt(raw.checkedAt), data: raw.data as UpdateInfo }
  } catch {
    return null
  }
}

/**
 * 当前版本号：
 * - 桌面端取 Tauri 应用版本（与安装包一致）；
 * - 网页端取后端 /api/health（与 sidecar 一致）；
 * - 后端不可达时回退到前端构建版本，绝不拿上次检查缓存里的旧版本顶替。
 */
export async function getCurrentVersion(): Promise<string> {
  const invoke = tauriInvoke()
  if (invoke && isDesktopApp()) {
    try {
      const version = await invoke('plugin:app|version')
      if (version) return String(version)
    } catch {
      /* 回退到后端版本 */
    }
  }
  try {
    const res = await fetch('/api/health', {
      signal: AbortSignal.timeout(1500),
    })
    const data = await res.json().catch(() => ({}))
    if (res.ok && data?.version) return String(data.version)
  } catch {
    /* 后端没起/离线：用前端构建版本兜底 */
  }
  return BUNDLED_VERSION
}

interface AndroidUpdatePayload {
  current?: string
  latest?: string
  has_update?: boolean
  notes?: string
  pub_date?: string
  url?: string
  fallback_urls?: string[]
  sha256?: string
  size?: number
  source?: string
}

function tauriInvoke(): TauriInternals['invoke'] | undefined {
  if (typeof window === 'undefined') return undefined
  return (window as any).__TAURI_INTERNALS__?.invoke
}

async function checkAndroidUpdate(): Promise<UpdateInfo> {
  const invoke = tauriInvoke()
  if (!invoke) throw new Error('当前环境不支持检查更新')
  const raw = ((await invoke('check_apk_update', {
    endpoints: ANDROID_UPDATE_ENDPOINTS,
  })) || {}) as AndroidUpdatePayload
  return {
    current: String(raw.current || ''),
    latest: String(raw.latest || ''),
    has_update: Boolean(raw.has_update),
    notes: String(raw.notes || ''),
    pub_date: String(raw.pub_date || ''),
    url: String(raw.url || ''),
    release_url: ANDROID_RELEASE_PAGE,
    source: raw.source ? String(raw.source) : undefined,
    fallback_urls: Array.isArray(raw.fallback_urls) ? raw.fallback_urls : [],
    sha256: String(raw.sha256 || ''),
    size: Number(raw.size) || 0,
  }
}

/** force=false 时后端可能直接返回当天缓存；force=true 会重新拉取更新清单。 */
export async function checkUpdate(force = false): Promise<UpdateInfo> {
  let info: UpdateInfo
  if (isAndroidApp()) {
    // 安卓壳连的是电脑后端，桌面版的版本号不代表 APK 版本，必须走原生检查
    info = await checkAndroidUpdate()
  } else if (isDesktopApp()) {
    // 桌面端用 Tauri 更新器检查：它拿安装包自身的版本与 Gitee 清单比较，
    // 和「帮助 → 检查更新」同源，不会因 sidecar 版本不一致而漏报。
    const invoke = tauriInvoke()
    if (!invoke) throw new Error('当前环境不支持检查更新')
    const raw = ((await invoke('check_desktop_update')) || {}) as UpdateInfo
    info = {
      current: String(raw.current || ''),
      latest: String(raw.latest || ''),
      has_update: Boolean(raw.has_update),
      notes: String(raw.notes || ''),
      pub_date: String(raw.pub_date || ''),
      url: String(raw.url || ''),
      release_url: String(raw.release_url || ANDROID_RELEASE_PAGE),
      source: raw.source ? String(raw.source) : undefined,
    }
  } else {
    const res = await fetch(`/api/update${force ? '?force=1' : ''}`)
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
    if (data.error) throw new Error(String(data.error))
    info = data as UpdateInfo
  }
  // 只向上更新：清单里的版本不比当前版本高时，不能显示为「可更新」。
  info = { ...info, has_update: info.has_update && isNewerVersion(info.latest, info.current) }
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
  const value = normalizeCheckedAt(ts)
  if (!value) return '尚未检查'
  try {
    return new Date(value).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return '刚刚'
  }
}

/**
 * 把更新说明拆成条目：去掉行首的 markdown 记号（- • * #、序号、勾选框），
 * 空行丢弃；没有结构化行时整段作为一条展示。
 */
export function parseUpdateNotes(notes: string): string[] {
  const text = String(notes || '').trim()
  if (!text) return []
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*(?:#{1,4}\s+|[-•*·]\s+|\d+[.、)]\s*|\[[x ]\]\s*)/, '').trim())
    .filter(Boolean)
  return lines.length ? lines : [text]
}

interface TauriInternals {
  invoke?: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>
}

/** 是否运行在 Android 壳里（Tauri 注入 + Android UA）。 */
export function isAndroidApp(): boolean {
  if (typeof window === 'undefined') return false
  if (!/Android/i.test(navigator.userAgent)) return false
  return Boolean((window as any).__TAURI_INTERNALS__?.invoke)
}

/** 是否运行在桌面客户端里（网页端不含 Tauri 注入对象）。 */
export function isDesktopApp(): boolean {
  if (typeof window === 'undefined') return false
  if (
    import.meta.env.DEV &&
    new URLSearchParams(window.location.search).get('desktop') === '1'
  ) {
    // 仅开发环境用于预览桌面端功能；生产构建会被静态替换掉。
    return true
  }
  // 安卓壳也是 Tauri，但更新走原生 APK 安装器，不能弹桌面更新器
  if (isAndroidApp()) return false
  return Boolean((window as any).__TAURI_INTERNALS__?.invoke)
}

/** 是否支持应用内更新（桌面版安装包 / Android APK 都支持）。 */
export function canInstallInApp(): boolean {
  return isDesktopApp() || isAndroidApp()
}

/** 订阅桌面安装器上报的进度；非桌面环境返回空解除函数。 */
export async function watchUpdateProgress(
  handler: (progress: UpdateProgress) => void,
): Promise<() => void> {
  if (!isDesktopApp()) return () => {}
  try {
    const { listen } = await import('@tauri-apps/api/event')
    return await listen<UpdateProgress>(UPDATE_PROGRESS_EVENT, (event) => {
      handler(event.payload)
    })
  } catch {
    return () => {}
  }
}

/**
 * 桌面版交给 Tauri 更新器：重新检查、下载并启动安装程序；
 * Android 走原生插件：下载 APK 后调起系统安装器。
 *
 * onProgress 会依次收到 checking / downloading（带百分比）/ installing / launching。
 * 安装器启动后应用会自动退出，invoke 的 Promise 可能随 WebView 销毁被拒绝，
 * 此时只要已经收到 launching 事件，就按成功处理，不再显示「更新失败」。
 */
export async function installUpdateInApp(
  info?: UpdateInfo,
  onProgress?: (progress: UpdateProgress) => void,
): Promise<string> {
  const invoke = tauriInvoke()
  if (!invoke) {
    throw new Error('当前环境不支持自动安装，请下载安装包')
  }
  if (isAndroidApp()) {
    if (!info?.url) throw new Error('缺少安装包下载地址')
    await invoke('install_apk_update', {
      url: info.url,
      fallbackUrls: info.fallback_urls ?? [],
      sha256: info.sha256 ?? '',
    })
    return String(info.latest || '')
  }

  let launched = false
  let launchedVersion = ''
  const unlisten = await watchUpdateProgress((progress) => {
    // 帮助菜单或后台预下载的 launching 不代表本次设置页发起的安装已完成。
    if (
      progress.stage === 'launching' &&
      (!progress.source || progress.source === 'manual')
    ) {
      launched = true
      launchedVersion = String(progress.version || '')
    }
    onProgress?.(progress)
  })
  try {
    // 后台已经把安装包下好了：直接启动安装器，省掉点击后的下载等待。
    try {
      const version = await invoke('install_prepared_update')
      return String(version ?? '') || launchedVersion
    } catch (error) {
      // 只有「还没准备好」才回退到完整更新流程；真正的安装失败要如实抛出。
      const message = typeof error === 'string' ? error : String((error as any)?.message || '')
      if (message !== 'not-ready') throw error
    }
    const version = await invoke('install_update')
    return String(version ?? '') || launchedVersion
  } catch (error) {
    if (launched) return launchedVersion
    throw error
  } finally {
    unlisten()
  }
}

