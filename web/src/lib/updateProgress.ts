/**
 * 设置页更新进度的合并规则：只接受本次主动更新（manual）产生的事件，
 * 并让下载百分比单调向前，避免后台预下载与手动下载两路进度交替时来回跳。
 */

export interface UpdateProgressEvent {
  stage: string
  version?: string
  downloaded?: number
  total?: number
  percent?: number
  message?: string
  source?: string
}

export function mergeInstallProgress<T extends UpdateProgressEvent>(
  prev: T | null,
  next: T,
): T | null {
  // 帮助菜单与后台预下载也会广播同一事件，混进来会让进度回退或阶段错乱。
  if (next.source && next.source !== 'manual') return prev
  if (next.stage === 'downloading' && prev?.stage === 'downloading') {
    const prevPercent = Number(prev.percent) || 0
    const nextPercent = Number(next.percent) || 0
    // 总大小后到或事件乱序时只保留更大的百分比。
    if (nextPercent < prevPercent) {
      return { ...next, percent: prevPercent }
    }
  }
  return next
}