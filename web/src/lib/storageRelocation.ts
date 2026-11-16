/**
 * 文献库迁移任务的本地句柄。
 *
 * 迁移在 sidecar 后台线程执行，和设置页生命周期无关；把 job_id 存在
 * localStorage 里，用户切去阅读页再回来、甚至刷新界面后都能接上进度。
 */

const JOB_KEY = 'paperead.storageRelocateJob'

/** 迁移失败时广播：设置页打开着就显示原因，没打开也不会丢。 */
export const RELOCATE_FAILED_EVENT = 'paperead:storage-relocate-failed'

export function loadRelocateJobId(): string | null {
  try {
    return localStorage.getItem(JOB_KEY) || null
  } catch {
    return null
  }
}

export function saveRelocateJobId(jobId: string): void {
  try {
    localStorage.setItem(JOB_KEY, jobId)
  } catch {
    // 隐私模式写不了：本次会话内仍可通过 active 接口恢复
  }
}

export function clearRelocateJobId(): void {
  try {
    localStorage.removeItem(JOB_KEY)
  } catch {
    // 忽略：下次查询 active 时也会发现任务已结束
  }
}

export function notifyRelocateFailed(message: string): void {
  window.dispatchEvent(
    new CustomEvent(RELOCATE_FAILED_EVENT, { detail: message || '迁移失败' }),
  )
}