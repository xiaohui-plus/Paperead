/**
 * 网页检索 provider：默认继承 DSH 官方 DeepSeek 搜索，失败回退本地检索。
 *
 * DSH 的 `@deepseek-ai/dsh-web-search-deepseek` 每次搜索是一次正式的
 * Messages 请求（带 `web_search` 服务端工具），返回结构化
 * `web_search_tool_result`。这是 DSH 的官方能力，默认优先使用：
 *
 *   1) 官方 DeepSeek 搜索（经本机 Python 代理，真实 Key 不出 Python）
 *   2) 官方不可用（断网、非 DeepSeek 供应商、额度/网关失败）时，回退到
 *      产品自带的免密钥检索（360 / Bing / DDG + OpenAlex），保证离线或
 *      受限网络下仍能给出候选来源
 *
 * 设置 `PAPEREAD_WEB_SEARCH_OFFICIAL=0` 可跳过官方、只用本地检索；
 * 设置 `PAPEREAD_WEB_SEARCH=0` 可整体关闭搜索 provider。
 */
import { DeepSeekSearchProvider } from '@deepseek-ai/dsh-web-search-deepseek'
import { WebError } from '@deepseek-ai/dsh-web'

export const name = 'paperead-web-search'
export const inject = ['web']

const DEFAULT_BASE = 'http://127.0.0.1:8010'
const DEFAULT_LIMIT = 6

function apiBase() {
  return (process.env.PAPEREAD_API_BASE || DEFAULT_BASE).replace(/\/$/, '')
}

function internalToken() {
  return process.env.PAPEREAD_INTERNAL_TOKEN || ''
}

function projectSource(source) {
  if (!source || typeof source !== 'object' || typeof source.url !== 'string') return null
  const url = source.url.trim()
  if (!url) return null
  const item = { url }
  if (typeof source.title === 'string' && source.title) item.title = source.title
  if (typeof source.snippet === 'string' && source.snippet) item.snippet = source.snippet
  if (typeof source.publishedAt === 'string' && source.publishedAt) {
    item.publishedAt = source.publishedAt
  }
  return item
}

/** 官方 provider 的配置在每次搜索时重新解析，跟随研究页当前模型设置。 */
function officialOptions() {
  return {
    apiKey: process.env.PAPEREAD_LLM_KEY || '',
    baseURL: `${apiBase()}/api/llm-proxy/anthropic/v1`,
    model: process.env.PAPEREAD_MODEL || 'deepseek-flash',
    apiVersion: '2023-06-01',
    maxTokens: 4096,
    maxUses: 5,
  }
}

class LocalSearchProvider {
  id = 'paperead-local'

  available() {
    return true
  }

  async search(request, signal) {
    const query = String(request?.query || '').trim()
    if (!query) return { sources: [], truncated: false }
    const limit = Number(request?.maxResults) > 0 ? Number(request.maxResults) : DEFAULT_LIMIT
    let response
    try {
      response = await fetch(`${apiBase()}/internal/research/web-search`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-internal-token': internalToken(),
        },
        body: JSON.stringify({ query, limit }),
        signal,
      })
    } catch (error) {
      if (signal?.aborted) throw error
      throw new WebError(`web search failed: ${String(error)}`, 'WEB_PROVIDER_ERROR', {
        cause: error,
      })
    }
    if (!response.ok) {
      const detail = (await response.text().catch(() => '')).slice(0, 500)
      throw new WebError(
        `web search failed: HTTP ${response.status} ${detail}`,
        'WEB_PROVIDER_ERROR',
      )
    }
    let payload
    try {
      payload = await response.json()
    } catch (error) {
      throw new WebError(
        `web search returned invalid JSON: ${String(error)}`,
        'WEB_PROVIDER_ERROR',
        { cause: error },
      )
    }
    const raw = Array.isArray(payload?.sources) ? payload.sources : []
    const sources = raw.map(projectSource).filter(Boolean).slice(0, limit)
    return { sources, truncated: raw.length > sources.length }
  }
}

/** 官方优先、本地兜底；官方失败不直接暴露给模型，只有两者都失败才报错。 */
class HybridSearchProvider {
  id = 'paperead'

  constructor(official, local) {
    this.official = official
    this.local = local
  }

  available() {
    return true
  }

  async search(request, signal) {
    const skipOfficial = process.env.PAPEREAD_WEB_SEARCH_OFFICIAL === '0'
    let officialError
    if (!skipOfficial) {
      try {
        return await this.official.search(request, signal)
      } catch (error) {
        if (signal?.aborted) throw error
        officialError = error
        console.warn(
          '[paperead-web-search] 官方 DeepSeek 搜索失败，回退本地检索：',
          String(error?.message || error).slice(0, 200),
        )
      }
    }
    try {
      return await this.local.search(request, signal)
    } catch (localError) {
      const detail = [
        officialError ? `official: ${String(officialError.message || officialError)}` : '',
        `local: ${String(localError.message || localError)}`,
      ]
        .filter(Boolean)
        .join('; ')
      throw new WebError(`web search failed (${detail})`, 'WEB_PROVIDER_ERROR', {
        cause: localError,
      })
    }
  }
}

export function apply(ctx) {
  if (process.env.PAPEREAD_WEB_SEARCH === '0') return
  const official = new DeepSeekSearchProvider(officialOptions)
  ctx.web.registerSearchProvider(new HybridSearchProvider(official, new LocalSearchProvider()))
}