import { browser } from 'wxt/browser'
import type { PaperMetadata } from './types'

const MAX_BYTES = 100 * 1024 * 1024

function isPdf(bytes: Uint8Array): boolean {
  return (
    bytes.length >= 5 &&
    bytes[0] === 0x25 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x44 &&
    bytes[3] === 0x46 &&
    bytes[4] === 0x2d
  )
}

export async function ensureHostPermission(url: string): Promise<void> {
  const origin = `${new URL(url).origin}/*`
  const has = await browser.permissions.contains({ origins: [origin] })
  if (has) return
  const granted = await browser.permissions.request({ origins: [origin] })
  if (!granted) throw new Error('未授权访问该站点，无法下载 PDF')
}

async function fetchInBackground(url: string): Promise<Uint8Array> {
  const response = await fetch(url, { credentials: 'include', redirect: 'follow' })
  if (!response.ok) throw new Error(`PDF 下载失败（HTTP ${response.status}）`)
  const length = Number(response.headers.get('content-length') || 0)
  if (length > MAX_BYTES) throw new Error('PDF 超过 100MB，请手动导入')
  const bytes = new Uint8Array(await response.arrayBuffer())
  if (!isPdf(bytes)) {
    throw new Error('该 PDF 需要登录或验证，请下载后拖入 Paperead')
  }
  if (bytes.length > MAX_BYTES) throw new Error('PDF 超过 100MB，请手动导入')
  return bytes
}

async function fetchInTab(tabId: number, url: string): Promise<Uint8Array> {
  const [injected] = await browser.scripting.executeScript({
    target: { tabId },
    func: async (target: string) => {
      const response = await fetch(target, { credentials: 'include' })
      if (!response.ok) return { error: `HTTP ${response.status}` }
      const bytes = new Uint8Array(await response.arrayBuffer())
      if (bytes.length > 30 * 1024 * 1024) return { error: 'too-large' }
      if (
        bytes.length < 5 ||
        bytes[0] !== 0x25 ||
        bytes[1] !== 0x50 ||
        bytes[2] !== 0x44
      ) {
        return { error: 'not-pdf' }
      }
      let binary = ''
      for (let index = 0; index < bytes.length; index += 0x8000) {
        binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000))
      }
      return { base64: btoa(binary) }
    },
    args: [url],
  })
  const result = injected!.result as { base64?: string; error?: string }
  if (!result?.base64) {
    if (result?.error === 'too-large') {
      throw new Error('PDF 较大，请下载后拖入 Paperead')
    }
    throw new Error(
      result?.error === 'not-pdf'
        ? '该 PDF 需要登录或验证，请下载后拖入 Paperead'
        : 'PDF 获取失败',
    )
  }
  const binary = atob(result.base64)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }
  return bytes
}

export async function acquirePdf(
  meta: PaperMetadata,
  tabId?: number,
): Promise<Uint8Array> {
  if (!meta.pdf_url) throw new Error('没有找到 PDF 地址')
  await ensureHostPermission(meta.pdf_url)
  try {
    return await fetchInBackground(meta.pdf_url)
  } catch (error) {
    if (!tabId) throw error
    return fetchInTab(tabId, meta.pdf_url)
  }
}
