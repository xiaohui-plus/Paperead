import { isDesktopApp } from './update'

export interface BrowserInfo {
  id: string
  name: string
  /** chromium | firefox */
  family: string
  path: string
  extensions_url: string
}

export interface ExtensionSetup {
  version: string
  chromium_dir: string | null
  firefox_dir: string | null
  browsers: BrowserInfo[]
}

interface TauriInternals {
  invoke?: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>
}

function invoke(): TauriInternals['invoke'] | undefined {
  if (!isDesktopApp()) return undefined
  return (window as any).__TAURI_INTERNALS__?.invoke
}

/** 读取本机浏览器与已释放的扩展目录；网页端返回 null。 */
export async function loadExtensionSetup(): Promise<ExtensionSetup | null> {
  const call = invoke()
  if (!call) return null
  try {
    const raw = (await call('browser_extension_setup')) as ExtensionSetup
    return {
      version: String(raw?.version || ''),
      chromium_dir: raw?.chromium_dir || null,
      firefox_dir: raw?.firefox_dir || null,
      browsers: Array.isArray(raw?.browsers) ? raw.browsers : [],
    }
  } catch (error) {
    console.warn('[extension] setup unavailable', error)
    return null
  }
}

/** 用指定浏览器打开它的扩展管理页。 */
export async function openBrowserExtensions(id: string): Promise<void> {
  const call = invoke()
  if (!call) throw new Error('网页版无法直接打开浏览器扩展页，请手动打开扩展管理页')
  await call('open_browser_extensions', { id })
}

/** 在资源管理器里打开扩展目录，方便用户在选择框里直接定位。 */
export async function openExtensionFolder(family: string): Promise<void> {
  const call = invoke()
  if (!call) throw new Error('网页版无法打开本地目录')
  await call('open_extension_folder', { family })
}

/** 复制文本；WebView 里优先用剪贴板 API，失败再退回 execCommand。 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const area = document.createElement('textarea')
      area.value = text
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(area)
      return ok
    } catch {
      return false
    }
  }
}