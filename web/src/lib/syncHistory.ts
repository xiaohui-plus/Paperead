/**
 * 桌面端记录哪些论文已经导出过同步包，默认只发送新增论文。
 * 只存在本机 localStorage；换机或清缓存后重新全选即可。
 */

const KEY = 'paperead.sync.exported'

export interface ExportHistory {
  [paperId: string]: number
}

export function loadExportHistory(): ExportHistory {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, unknown>
    const result: ExportHistory = {}
    for (const [id, value] of Object.entries(parsed)) {
      const stamp = Number(value)
      if (Number.isFinite(stamp) && stamp > 0) result[id] = stamp
    }
    return result
  } catch {
    return {}
  }
}

export function markExported(ids: string[]): void {
  if (!ids.length) return
  const history = loadExportHistory()
  const now = Date.now()
  for (const id of ids) history[id] = now
  try {
    localStorage.setItem(KEY, JSON.stringify(history))
  } catch {
    // 写不了就只在本次会话内按“已发送”展示
  }
}

export function clearExportHistory(): void {
  try {
    localStorage.removeItem(KEY)
  } catch {
    // 忽略
  }
}