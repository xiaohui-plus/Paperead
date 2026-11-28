/**
 * 带内网拦截的 web_fetch provider。
 *
 * DSH 官方的 `dsh-web-fetch-http` 明确把 SSRF 防护列为未实现（README 说
 * 该 provider「是一个 SSRF primitive」），而桌面端的研究 agent 就在用户
 * 机器上，能直接访问 localhost 服务与局域网设备。这里复用官方 provider
 * 的抓取实现（URL 校验、同源重定向、字节/字符上限、超时、编码识别），
 * 只在外层加一道「只允许公网目标」的闸门，避免模型把内部服务当网页抓。
 */
import { lookup } from 'node:dns/promises'
import { isIP } from 'node:net'

import { WebError } from '@deepseek-ai/dsh-web'
import {
  HttpFetchProvider,
  LOCAL_FETCH_PROVIDER_ID,
} from '@deepseek-ai/dsh-web-fetch-http'

export const name = 'paperead-web-fetch'
export const inject = ['web']

const BLOCKED_HOST_SUFFIXES = ['.localhost', '.local', '.internal', '.lan', '.home.arpa']

function isPrivateIpv4(address) {
  const parts = address.split('.').map((part) => Number(part))
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part))) return true
  const [a, b, c] = parts
  if (a === 0 || a === 10 || a === 127) return true
  if (a === 169 && b === 254) return true
  if (a === 172 && b >= 16 && b <= 31) return true
  if (a === 192 && b === 168) return true
  if (a === 100 && b >= 64 && b <= 127) return true
  if (a === 198 && (b === 18 || b === 19)) return true
  if (a === 192 && b === 0 && (c === 0 || c === 2)) return true
  if (a === 198 && b === 51 && c === 100) return true
  if (a === 203 && b === 0 && c === 113) return true
  if (a >= 224) return true
  return false
}

function isPrivateIpv6(address) {
  const value = address.toLowerCase()
  if (value === '::' || value === '::1') return true
  if (value.startsWith('::ffff:')) return isPrivateIpv4(value.slice('::ffff:'.length))
  if (/^(fc|fd)/.test(value)) return true
  if (/^fe[89ab]/.test(value)) return true
  if (/^ff/.test(value)) return true
  if (value.startsWith('2001:db8:')) return true
  return false
}

function isPrivateAddress(address) {
  const version = isIP(address)
  if (version === 4) return isPrivateIpv4(address)
  if (version === 6) return isPrivateIpv6(address)
  return true
}

function blocked(message) {
  return new WebError(message, 'WEB_BLOCKED_URL')
}

async function assertPublicUrl(raw) {
  let url
  try {
    url = new URL(raw)
  } catch (error) {
    throw new WebError(`invalid URL: ${raw}`, 'WEB_INVALID_URL', { cause: error })
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new WebError(`unsupported URL scheme "${url.protocol}"`, 'WEB_INVALID_URL')
  }
  if (url.username || url.password) {
    throw blocked('credentials in URLs are not allowed')
  }

  const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase()
  if (!host) throw blocked('URL has no host')
  if (host === 'localhost' || BLOCKED_HOST_SUFFIXES.some((suffix) => host.endsWith(suffix))) {
    throw blocked(`local host is not reachable: ${host}`)
  }
  if (isIP(host)) {
    if (isPrivateAddress(host)) throw blocked(`private address is not reachable: ${host}`)
    return url
  }

  // 域名解析到的每个地址都要是公网地址；解析失败按不可达处理。
  let records
  try {
    records = await lookup(host, { all: true, verbatim: true })
  } catch (error) {
    throw new WebError(`cannot resolve host: ${host}`, 'WEB_PROVIDER_ERROR', { cause: error })
  }
  if (!records.length) throw blocked(`host does not resolve: ${host}`)
  for (const record of records) {
    if (isPrivateAddress(record.address)) {
      throw blocked(`host resolves to a private address: ${host} -> ${record.address}`)
    }
  }
  return url
}

export class GuardedFetchProvider {
  id = 'paperead-http'

  constructor(inner) {
    this.inner = inner
  }

  available() {
    return true
  }

  /**
   * 学术检索结果大量是 doi.org / 短链，会跨域跳到出版社；官方 provider
   * 按安全策略拒绝跨域重定向。这里在「已经因跨域失败」时补一次受控解析：
   * 用不跟随的 GET 取 Location（读完头就取消正文），逐跳校验必须是公网，
   * 再对最终地址重新抓取。正常路径没有额外请求；跨域之外的错误原样抛出。
   */
  async resolveRedirect(url, signal) {
    let current = url
    for (let hop = 0; hop < 3; hop += 1) {
      let response
      try {
        response = await fetch(current, {
          method: 'GET',
          redirect: 'manual',
          signal,
          headers: { 'user-agent': 'paperead-research/1.0' },
        })
      } catch {
        return null
      }
      const status = response.status
      const location = response.headers.get('location')
      await response.body?.cancel().catch(() => {})
      if (![301, 302, 303, 307, 308].includes(status) || !location) return current
      let next
      try {
        next = new URL(location, current)
      } catch {
        return null
      }
      await assertPublicUrl(next.href)
      current = next.href
    }
    return current
  }

  async fetch(request, signal) {
    await assertPublicUrl(request.url)
    let url = request.url
    // 出版社常见多级跳转（doi.org -> www.nature.com -> idp.nature.com），
    // 逐层解析后重试；每次跳转目标仍要过公网校验。
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        return await this.inner.fetch({ ...request, url }, signal)
      } catch (error) {
        if (error?.code !== 'WEB_REDIRECT_BLOCKED' || attempt === 2) throw error
        const target = await this.resolveRedirect(url, signal)
        if (!target || target === url) throw error
        url = target
      }
    }
    throw blocked(`too many redirects: ${request.url}`)
  }
}

export function apply(ctx) {
  if (process.env.PAPEREAD_WEB_FETCH === '0') return
  const inner = new HttpFetchProvider({
    maxUrlLength: 2048,
    maxResponseBytes: 5_000_000,
    maxBodyChars: 100_000,
    timeoutMs: 60_000,
    maxRedirects: 3,
    userAgent: `paperead-research/${process.env.PAPEREAD_VERSION || '0.0.0'}`,
  })
  ctx.web.registerFetchProvider(new GuardedFetchProvider(inner))
}

export const LOCAL_FETCH_ID = LOCAL_FETCH_PROVIDER_ID