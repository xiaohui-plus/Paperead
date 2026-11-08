/** 检索页的 URL 即状态：刷新、前进后退、从阅读页返回都能还原。 */

import {
  FIELD_META,
  PER_PAGE,
  SORT_META,
  SOURCE_META,
  type ScholarSource,
  type SearchParams,
} from './scholar'

const DEFAULT_PER_PAGE = 20

function isSource(value: string | null): value is ScholarSource {
  return SOURCE_META.some((s) => s.k === value)
}

function pick<T extends string>(value: string | null, allowed: readonly { k: T }[], fallback: T): T {
  return allowed.some((item) => item.k === value) ? (value as T) : fallback
}

function year(value: string | null): number | null {
  if (!value || !/^\d{4}$/.test(value)) return null
  const parsed = Number(value)
  return parsed >= 1000 && parsed <= 2200 ? parsed : null
}

export function readParams(search = window.location.search): SearchParams {
  const sp = new URLSearchParams(search)
  const perPage = Number(sp.get('per'))
  return {
    q: sp.get('q') ?? '',
    source: isSource(sp.get('src')) ? (sp.get('src') as ScholarSource) : 'arxiv',
    sort: pick(sp.get('sort'), SORT_META, 'rel'),
    page: Math.max(0, Number(sp.get('page')) || 0),
    field: pick(sp.get('field'), FIELD_META, 'all'),
    phrase: sp.get('phrase') === '1',
    category: sp.get('cat') ?? '',
    fromYear: year(sp.get('from')),
    toYear: year(sp.get('to')),
    perPage: (PER_PAGE as readonly number[]).includes(perPage) ? perPage : DEFAULT_PER_PAGE,
  }
}

export function readPid(search = window.location.search): string | null {
  const value = new URLSearchParams(search).get('pid')
  return value && value.length > 2 ? value : null
}

/** 把检索状态写回地址栏。详情开关用 push，筛选变化用 replace，避免历史记录爆炸。 */
export function writeParams(
  params: SearchParams,
  pid: string | null,
  mode: 'push' | 'replace' = 'replace',
): void {
  const url = new URL(window.location.href)
  url.searchParams.set('v', 'search')

  const setOrDelete = (key: string, value: string | null) => {
    if (value) url.searchParams.set(key, value)
    else url.searchParams.delete(key)
  }

  setOrDelete('q', params.q.trim() || null)
  setOrDelete('src', params.source)
  setOrDelete('sort', params.sort === 'rel' ? null : params.sort)
  setOrDelete('page', params.page > 0 ? String(params.page) : null)
  setOrDelete('field', params.field === 'all' ? null : params.field)
  setOrDelete('phrase', params.phrase ? '1' : null)
  setOrDelete('cat', params.category.trim() || null)
  setOrDelete('from', params.fromYear ? String(params.fromYear) : null)
  setOrDelete('to', params.toYear ? String(params.toYear) : null)
  setOrDelete('per', params.perPage !== DEFAULT_PER_PAGE ? String(params.perPage) : null)
  setOrDelete('pid', pid)

  if (mode === 'push') window.history.pushState({}, '', url)
  else window.history.replaceState({}, '', url)
}
