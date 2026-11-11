/**
 * 设备同步：电脑端把「服务器地址 + 访问口令 + 模型配置」做成二维码，
 * 手机端扫码后直接写入本机，之后就能独立同步数据、离线阅读。
 */
import JSZip from 'jszip'
import { apiFetch, normalizeServerBase } from './mobile'
import {
  deleteOffline,
  downloadPaper,
  importOfflineRecord,
  listOffline,
  pageImagePath,
  type OfflineAsset,
  type OfflineRecord,
} from './offlineStore'

export interface SyncInfo {
  base: string
  token: string
  /** 电脑是否已对局域网开放；false 时手机连不上 */
  exposed: boolean
  lanIps: string[]
  host: string
  port: number
  name: string
  version: string
}

export interface SyncPayload {
  base: string
  token: string
  settings?: Record<string, unknown>
}

export async function fetchSyncInfo(): Promise<SyncInfo> {
  const response = await apiFetch('/api/sync/info')
  if (!response.ok) throw new Error(`读取同步信息失败（HTTP ${response.status}）`)
  const data = (await response.json()) as Record<string, unknown>
  return {
    base: String(data.base ?? ''),
    token: String(data.token ?? ''),
    exposed: data.exposed === true,
    lanIps: Array.isArray(data.lan_ips) ? data.lan_ips.map(String) : [],
    host: String(data.host ?? ''),
    port: Number(data.port ?? 0),
    name: String(data.name ?? ''),
    version: String(data.version ?? ''),
  }
}

/** 电脑端已保存的模型配置；读不到时二维码里不带配置，手机仍可正常配对。 */
export async function fetchServerModelSettings(): Promise<Record<string, unknown> | null> {
  try {
    const response = await apiFetch('/api/settings/models')
    if (!response.ok) return null
    const data = (await response.json()) as { settings?: unknown }
    return data.settings && typeof data.settings === 'object'
      ? (data.settings as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

export function buildSyncCode(
  info: Pick<SyncInfo, 'base' | 'token'>,
  settings: Record<string, unknown> | null,
): string {
  return JSON.stringify({
    v: 1,
    base: info.base,
    token: info.token,
    ...(settings ? { settings } : {}),
  })
}

/** 兼容 JSON、paperead:// 链接、`地址|口令` 三种写法。 */
export function parseSyncCode(raw: string): SyncPayload | null {
  const text = raw.trim()
  if (!text) return null

  try {
    const data = JSON.parse(text) as Record<string, unknown>
    if (data && typeof data === 'object') {
      const base = data.base ?? data.url ?? data.server
      if (typeof base === 'string' && base.trim()) {
        return {
          base: normalizeServerBase(base),
          token: typeof data.token === 'string' ? data.token : '',
          settings:
            data.settings && typeof data.settings === 'object'
              ? (data.settings as Record<string, unknown>)
              : undefined,
        }
      }
    }
  } catch {
    // 不是 JSON，继续尝试其它格式
  }

  if (text.startsWith('paperead://') || /^https?:\/\//i.test(text)) {
    try {
      const url = new URL(
        text.startsWith('paperead://') ? text.replace('paperead://', 'http://') : text,
      )
      const base = url.searchParams.get('base') ?? `${url.protocol}//${url.host}`
      const token =
        url.searchParams.get('token') ?? url.searchParams.get('pr_token') ?? ''
      return { base: normalizeServerBase(base), token }
    } catch {
      return null
    }
  }

  const parts = text.split('|')
  if (parts.length >= 2) {
    try {
      return { base: normalizeServerBase(parts[0]), token: parts[1].trim() }
    } catch {
      return null
    }
  }
  return null
}

// ── 文献同步：把电脑端的解析结果和 PDF 拉到手机离线库 ────────────

export interface RemotePaper {
  id: string
  title: string
  authors: string
  pages: number
  size: number
  /** uploaded / parsing / ready / failed */
  status: string
  thumbUrl: string | null
}

/** 电脑端文献列表（只读，用于选择要拉取哪些）。 */
export async function fetchRemotePapers(): Promise<RemotePaper[]> {
  const response = await apiFetch('/api/papers')
  if (!response.ok) throw new Error(`读取电脑端文献失败（HTTP ${response.status}）`)
  const data = (await response.json().catch(() => ({}))) as { papers?: unknown }
  if (!Array.isArray(data.papers)) return []
  return data.papers
    .map((value) => {
      const row = value as Record<string, unknown>
      return {
        id: String(row.id ?? ''),
        title: String(row.title ?? '未命名'),
        authors: String(row.authors ?? ''),
        pages: Number(row.pages ?? 0),
        size: Number(row.size ?? 0),
        status: String(row.status ?? ''),
        thumbUrl: typeof row.thumb_url === 'string' ? row.thumb_url : null,
      }
    })
    .filter((item) => item.id)
}

export type SyncMode = 'full' | 'select' | 'merge'

/** 电脑上 Scholaread（学术阅读）的数据概况，用于手机端提示可导入多少篇。 */
export interface ScholarReadInfo {
  ok: boolean
  total: number
  withPdf: number
  running: boolean
  error: string
}

/** 查询电脑上的 Scholaread 是否可用、有多少篇已下载的文献。 */
export async function fetchScholarReadInfo(): Promise<ScholarReadInfo> {
  const response = await apiFetch('/api/scholaread/status')
  if (!response.ok) throw new Error(`检测电脑端学术阅读失败（HTTP ${response.status}）`)
  const data = (await response.json().catch(() => ({}))) as Record<string, unknown>
  return {
    ok: data.ok === true,
    total: Number(data.total ?? 0),
    withPdf: Number(data.with_pdf ?? 0),
    running: data.running === true,
    error: String(data.error ?? ''),
  }
}

export interface ScholarReadImport {
  imported: number
  linked: number
  skippedNoPdf: number
  running: boolean
  errors: string[]
}

/**
 * 让电脑把 Scholaread 里已下载的文献导入 Paperead 文库。
 * 只拉不推：手机端永远不改动电脑上 Scholaread 的数据和阅读进度。
 */
export async function importScholarReadLibrary(): Promise<ScholarReadImport> {
  const response = await apiFetch('/api/scholaread/sync', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pull: true, push: false, limit: 1000 }),
  })
  if (!response.ok) {
    throw new Error(`从电脑端学术阅读导入失败（HTTP ${response.status}）`)
  }
  const data = (await response.json().catch(() => ({}))) as Record<string, unknown>
  if (data.ok === false && data.error) throw new Error(String(data.error))
  const pull = (data.pull ?? {}) as Record<string, unknown>
  const errors = Array.isArray(pull.errors)
    ? pull.errors.map((item) => {
        const row = item as Record<string, unknown>
        return `${String(row.title ?? '未知文献')}：${String(row.error ?? '导入失败')}`
      })
    : []
  return {
    imported: Number(pull.imported ?? 0),
    linked: Number(pull.linked ?? 0),
    skippedNoPdf: Number(pull.skipped_no_pdf ?? 0),
    running: data.running === true,
    errors,
  }
}

export interface SyncReport {
  mode: SyncMode
  downloaded: number
  skipped: number
  removed: number
  failed: { id: string; title: string; reason: string }[]
  /** 本次同步前从电脑端学术阅读导入的数量（手机端「导入并拉取」） */
  scholaread?: { imported: number; linked: number; skippedNoPdf: number }
}

export interface SyncOptions {
  mode: SyncMode
  /** select 模式下勾选的论文 id */
  ids?: string[]
  /** 每篇处理完（或开始处理）时回调，done 为已完成数量 */
  onProgress?: (info: { done: number; total: number; title: string }) => void
}

/**
 * 三种同步方式：
 * - full：以电脑为准，下载全部论文，并删掉手机上有、电脑上没有的论文副本
 *   （StoryReader 导入的书不删，它本来就不属于电脑端）；
 * - select：只拉用户勾选的论文，已在本机的跳过；
 * - merge：并集合并，保留下载过的和导入的书，只补电脑端新增的论文。
 */
export async function syncFromServer(options: SyncOptions): Promise<SyncReport> {
  const [remote, local] = await Promise.all([fetchRemotePapers(), listOffline()])
  const localIds = new Set(local.map((item) => item.id))
  const remoteIds = new Set(remote.map((item) => item.id))

  let targets: RemotePaper[]
  if (options.mode === 'select') {
    const wanted = new Set(options.ids ?? [])
    targets = remote.filter((item) => wanted.has(item.id))
  } else if (options.mode === 'merge') {
    targets = remote.filter((item) => !localIds.has(item.id))
  } else {
    targets = remote
  }

  const report: SyncReport = {
    mode: options.mode,
    downloaded: 0,
    skipped: 0,
    removed: 0,
    failed: [],
  }

  for (let index = 0; index < targets.length; index += 1) {
    const paper = targets[index]
    options.onProgress?.({ done: index, total: targets.length, title: paper.title })
    if (options.mode !== 'full' && localIds.has(paper.id)) {
      report.skipped += 1
      continue
    }
    if (paper.status !== 'ready') {
      report.failed.push({ id: paper.id, title: paper.title, reason: '电脑端还在解析' })
      continue
    }
    try {
      await downloadPaper(paper.id)
      report.downloaded += 1
    } catch (error) {
      report.failed.push({
        id: paper.id,
        title: paper.title,
        reason: error instanceof Error ? error.message : '下载失败',
      })
    }
    options.onProgress?.({ done: index + 1, total: targets.length, title: paper.title })
  }

  // 全部拉取以电脑为准：下载完再清理手机上有、电脑上没有的论文副本，
  // 避免中途失败时把本机内容提前删掉；StoryReader 导入的书保留。
  if (options.mode === 'full') {
    for (const item of local) {
      if (item.source === 'storyreader' || remoteIds.has(item.id)) continue
      try {
        await deleteOffline(item.id)
        report.removed += 1
      } catch {
        // 删不掉的先留着，不影响同步结果
      }
    }
  }

  return report
}

// ── 离线同步包：电脑导出、手机导入，不依赖网络 ─────────────

export interface SyncPackageSkipped {
  id: string
  title: string
  reason: string
}

export interface SyncExportReport {
  ok: boolean
  file: string
  path: string
  url: string
  size: number
  count: number
  skipped: SyncPackageSkipped[]
}

export interface SyncImportProgress {
  done: number
  total: number
  title: string
}

export interface SyncImportReport {
  total: number
  imported: number
  updated: number
  failed: { title: string; reason: string }[]
  device?: string
  appVersion?: string
}

interface PackagePaper {
  id?: unknown
  title?: unknown
  authors?: unknown
  pages?: unknown
  files?: {
    blocks?: unknown
    pdf?: unknown
    thumb?: unknown
    pages?: Record<string, unknown>
    assets?: Record<string, unknown>
  }
}

interface PackageManifest {
  format?: unknown
  version?: unknown
  device?: unknown
  appVersion?: unknown
  papers?: unknown
}

/** 电脑端：把勾选的论文打成 .paperead 同步包。 */
export async function exportSyncPackage(ids: string[]): Promise<SyncExportReport> {
  const response = await apiFetch('/api/sync/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  })
  const data = (await response.json().catch(() => ({}))) as Record<string, unknown>
  if (!response.ok) {
    throw new Error(String(data.detail || `导出失败（HTTP ${response.status}）`))
  }
  return {
    ok: data.ok === true,
    file: String(data.file ?? ''),
    path: String(data.path ?? ''),
    url: String(data.url ?? ''),
    size: Number(data.size ?? 0),
    count: Number(data.count ?? 0),
    skipped: Array.isArray(data.skipped)
      ? data.skipped.map((item) => {
          const row = item as Record<string, unknown>
          return {
            id: String(row.id ?? ''),
            title: String(row.title ?? '未命名'),
            reason: String(row.reason ?? '未导出'),
          }
        })
      : [],
  }
}

/** 电脑端：把刚生成的同步包保存到用户选择的位置。 */
export async function saveSyncPackageFile(
  report: SyncExportReport,
): Promise<'saved' | 'cancelled' | 'downloaded'> {
  if (!report.url) throw new Error('同步包下载地址为空')
  const response = await apiFetch(report.url)
  if (!response.ok) throw new Error(`读取同步包失败（HTTP ${response.status}）`)

  type PickerOptions = {
    suggestedName?: string
    types?: { description?: string; accept?: Record<string, string[]> }[]
  }
  const picker = (
    window as Window & {
      showSaveFilePicker?: (
        options: PickerOptions,
      ) => Promise<{ createWritable: () => Promise<WritableStream<Uint8Array>> }>
    }
  ).showSaveFilePicker

  if (typeof picker === 'function' && response.body) {
    let handle: { createWritable: () => Promise<WritableStream<Uint8Array>> }
    try {
      handle = await picker({
        suggestedName: report.file || 'paperead-sync.paperead',
        types: [
          {
            description: 'Paperead 同步包',
            accept: { 'application/zip': ['.paperead'] },
          },
        ],
      })
    } catch (error) {
      if ((error as { name?: string })?.name === 'AbortError') return 'cancelled'
      throw error
    }
    await response.body.pipeTo(await handle.createWritable())
    return 'saved'
  }

  const blob = await response.blob()
  const href = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = href
  link.download = report.file || 'paperead-sync.paperead'
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(href), 1500)
  return 'downloaded'
}

async function readBlob(zip: JSZip, path: unknown): Promise<Blob | null> {
  if (typeof path !== 'string' || !path) return null
  const file = zip.file(path)
  if (!file) return null
  return file.async('blob')
}

/**
 * 手机端：导入电脑导出的 .paperead。包里的每篇论文直接写进离线库，
 * 手机上已有的同名论文会被覆盖。
 */
export async function importSyncPackage(
  file: File,
  onProgress?: (info: SyncImportProgress) => void,
): Promise<SyncImportReport> {
  if (file.size > 2 * 1024 * 1024 * 1024) {
    throw new Error('同步包超过 2 GB，请在电脑上分批导出后再导入')
  }
  const zip = await JSZip.loadAsync(file)
  const manifestFile = zip.file('manifest.json')
  if (!manifestFile) throw new Error('不是 Paperead 同步包：缺少 manifest.json')
  let manifest: PackageManifest
  try {
    manifest = JSON.parse(await manifestFile.async('string')) as PackageManifest
  } catch {
    throw new Error('同步包里的 manifest.json 已损坏')
  }
  if (manifest.format !== 'paperead-sync') {
    throw new Error('不是 Paperead 同步包')
  }
  const papers = Array.isArray(manifest.papers)
    ? (manifest.papers as PackagePaper[])
    : []
  if (papers.length === 0) throw new Error('同步包里没有论文')
  if (papers.length > 500) {
    throw new Error('同步包里的论文超过 500 篇，请分批导出后再导入')
  }

  const existing = new Set((await listOffline()).map((item) => item.id))
  const report: SyncImportReport = {
    total: papers.length,
    imported: 0,
    updated: 0,
    failed: [],
    device: String(manifest.device ?? ''),
    appVersion: String(manifest.appVersion ?? ''),
  }

  for (let index = 0; index < papers.length; index += 1) {
    const paper = papers[index]
    const id = typeof paper.id === 'string' ? paper.id : ''
    const title = String(paper.title || id || '未命名')
    onProgress?.({ done: index, total: papers.length, title })
    try {
      if (!id) throw new Error('缺少论文 id')
      const files = paper.files ?? {}
      const blocksPath = typeof files.blocks === 'string' ? files.blocks : ''
      const blocksFile = blocksPath ? zip.file(blocksPath) : null
      if (!blocksFile) throw new Error('缺少解析结果')
      const payload = JSON.parse(
        await blocksFile.async('string'),
      ) as OfflineRecord['payload']
      const pdf = await readBlob(zip, files.pdf)
      const thumb = await readBlob(zip, files.thumb)

      const assets: OfflineAsset[] = []
      if (files.assets && typeof files.assets === 'object') {
        for (const [url, zipPath] of Object.entries(files.assets)) {
          const blob = await readBlob(zip, zipPath)
          if (blob) assets.push({ path: url, blob })
        }
      }
      if (files.pages && typeof files.pages === 'object') {
        for (const [page, zipPath] of Object.entries(files.pages)) {
          const number = Number(page)
          if (!Number.isFinite(number) || number <= 0) continue
          const blob = await readBlob(zip, zipPath)
          if (blob) {
            assets.push({ path: pageImagePath(id, number), blob })
          }
        }
      }

      const bytes =
        new Blob([JSON.stringify(payload)]).size +
        (pdf?.size ?? 0) +
        (thumb?.size ?? 0) +
        assets.reduce((sum, asset) => sum + asset.blob.size, 0)
      const record: OfflineRecord = {
        id,
        title,
        authors: String(paper.authors || payload.authors || ''),
        payload,
        pdf,
        thumb,
        assets,
        bytes,
        updatedAt: Date.now(),
        source: 'paper',
      }
      await importOfflineRecord(record)
      if (existing.has(id)) report.updated += 1
      else report.imported += 1
      existing.add(id)
    } catch (error) {
      report.failed.push({
        title,
        reason: error instanceof Error ? error.message : '导入失败',
      })
    }
    onProgress?.({ done: index + 1, total: papers.length, title })
  }
  return report
}

