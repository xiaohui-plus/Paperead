import type { Paper } from '../types'
import { mediaUrl } from './mobile'

/**
 * 数据加载。
 *
 * - `demo`：读内置示例（web/public/demo/paper.json），不需要后端。
 * - 其它 id：读本地服务 `GET /api/papers/{id}/blocks`。
 * - 配置了 VITE_API_BASE_URL 时，所有请求走该地址（用于把前端单独部署）。
 */
const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? ''

async function getJson(url: string) {
  const res = await fetch(url)
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error((data as any).detail || `加载失败（HTTP ${res.status}）`)
  }
  return res.json()
}

export async function loadPaper(paperId = 'demo'): Promise<Paper> {
  if (paperId === 'demo') {
    return (await getJson('/demo/paper.json')) as Paper
  }
  const data = await getJson(`${API_BASE}/api/papers/${paperId}/blocks`)
  return {
    paper_id: paperId,
    title: data.title ?? '',
    title_zh: '',
    authors: data.authors ?? '',
    affiliation: '',
    page_count: data.page_count ?? 0,
    page_sizes: data.page_sizes ?? [],
    columns: data.columns ?? 1,
    glossary: data.glossary ?? {},
    translation_lang: data.translation_lang ?? '',
    progress: data.progress ?? { pct: 0, block_id: '', page: 0 },
    // 安卓壳跨源访问时图片需要绝对地址和访问口令
    blocks: (data.blocks ?? []).map((block: Paper['blocks'][number]) =>
      block.image_path ? { ...block, image_path: mediaUrl(block.image_path) } : block,
    ),
  } as Paper
}

export const isDemoMode = !API_BASE
