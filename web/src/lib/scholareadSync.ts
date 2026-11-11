/** 学术阅读（Scholaread）本机同步：设置与 API 客户端。 */

export interface ScholarReadSettings {
  /** 留空表示自动定位 %APPDATA%\scholaread */
  dataDir: string
}

export interface ScholarReadStatus {
  ok: boolean
  data_dir: string
  db_path: string
  exists: boolean
  schema_ok: boolean
  running: boolean | null
  total: number
  with_pdf: number
  notes: number
  error: string
}

export interface ScholarReadPull {
  scanned: number
  imported: number
  linked: number
  progress_updated: number
  skipped_no_pdf: number
  errors: { title: string; error: string }[]
}

export interface ScholarReadPaper {
  id: string
  title: string
  authors: string
  readPercent: number
  hasPdf: boolean
  imported: boolean
  paperId: string
  thumbUrl: string
}

export interface ScholarReadPaperList {
  ok: boolean
  data_dir: string
  running: boolean | null
  total: number
  items: ScholarReadPaper[]
  error: string
}

export interface ScholarReadPush {
  updated: number
  skipped: number
  backup: string
  error: string
}

export interface ScholarReadSyncResult {
  ok: boolean
  data_dir: string
  running: boolean | null
  pull: ScholarReadPull
  push: ScholarReadPush
  warnings: string[]
  duration_ms: number
  error: string
  /** 前端补上的完成时间 */
  at: number
}

const SETTINGS_KEY = 'paperead.scholaread'
const LAST_KEY = 'paperead.scholaread.last'
export const SCHOLAREAD_SYNCED = 'paperead:scholaread-sync'

const DEFAULTS: ScholarReadSettings = { dataDir: '' }

export function loadScholarReadSettings(): ScholarReadSettings {
  try {
    const raw = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}')
    return {
      dataDir: typeof raw.dataDir === 'string' ? raw.dataDir : '',
    }
  } catch {
    return { ...DEFAULTS }
  }
}

export function saveScholarReadSettings(patch: Partial<ScholarReadSettings>): ScholarReadSettings {
  const next = { ...loadScholarReadSettings(), ...patch }
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(next))
  window.dispatchEvent(new Event('paperead:scholaread-settings'))
  return next
}

export function loadLastScholarReadSync(): ScholarReadSyncResult | null {
  try {
    const raw = localStorage.getItem(LAST_KEY)
    return raw ? (JSON.parse(raw) as ScholarReadSyncResult) : null
  } catch {
    return null
  }
}

function saveLast(result: ScholarReadSyncResult) {
  localStorage.setItem(LAST_KEY, JSON.stringify(result))
}

export async function probeScholarRead(dataDir = ''): Promise<ScholarReadStatus> {
  const res = await fetch(
    `/api/scholaread/status?data_dir=${encodeURIComponent(dataDir)}`,
  )
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((data as any).detail || `HTTP ${res.status}`)
  return data as ScholarReadStatus
}

export async function listScholarReadPapers(
  dataDir = '',
): Promise<ScholarReadPaperList> {
  const res = await fetch(
    `/api/scholaread/papers?data_dir=${encodeURIComponent(dataDir)}&limit=500`,
  )
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((data as any).detail || `HTTP ${res.status}`)
  return data as ScholarReadPaperList
}

let syncing = false

export function isScholarReadSyncing(): boolean {
  return syncing
}

export async function runScholarReadSync(options?: {
  dataDir?: string
  pull?: boolean
  push?: boolean
  readingIds?: string[]
}): Promise<ScholarReadSyncResult | null> {
  if (syncing) return null
  syncing = true
  try {
    const settings = loadScholarReadSettings()
    const res = await fetch('/api/scholaread/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        data_dir: options?.dataDir ?? settings.dataDir,
        pull: options?.pull !== false,
        push: options?.push !== false,
        limit: 500,
        reading_ids: options?.readingIds ?? [],
      }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error((data as any).detail || `HTTP ${res.status}`)
    const result = { ...(data as ScholarReadSyncResult), at: Date.now() }
    saveLast(result)
    window.dispatchEvent(new Event(SCHOLAREAD_SYNCED))
    return result
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    const result: ScholarReadSyncResult = {
      ok: false,
      data_dir: '',
      running: null,
      pull: {
        scanned: 0,
        imported: 0,
        linked: 0,
        progress_updated: 0,
        skipped_no_pdf: 0,
        errors: [],
      },
      push: { updated: 0, skipped: 0, backup: '', error: '' },
      warnings: [],
      duration_ms: 0,
      error: message,
      at: Date.now(),
    }
    saveLast(result)
    return result
  } finally {
    syncing = false
  }
}

export function formatSyncTime(at: number): string {
  if (!at) return '尚未同步'
  const date = new Date(at)
  const now = new Date()
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  const time = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  return sameDay ? `今天 ${time}` : `${date.getMonth() + 1}月${date.getDate()}日 ${time}`
}
