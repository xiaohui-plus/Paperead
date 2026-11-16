/**
 * 文献库迁移的全局守望。
 *
 * 迁移任务在后端线程里跑，设置页关掉、切到阅读页都不会中断；这个组件挂在
 * 应用根部，负责跨页面接上进度、处理失败通知，并在复制完成后触发重启。
 * 这样「迁移时切界面」不会出现任务完成却没人重启、或进度丢失的情况。
 */
import { useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { getActiveRelocateJob, getRelocateStorageJob } from '../lib/library'
import {
  clearRelocateJobId,
  loadRelocateJobId,
  notifyRelocateFailed,
  saveRelocateJobId,
} from '../lib/storageRelocation'
import { isDesktopApp } from '../lib/update'

export function StorageRelocationWatcher() {
  const [finishing, setFinishing] = useState<{
    restarting: boolean
  } | null>(null)

  useEffect(() => {
    let stopped = false
    let polling = false
    let lastProbe = 0

    const tick = async () => {
      if (stopped || polling) return
      polling = true
      try {
        let jobId = loadRelocateJobId()
        if (!jobId) {
          // 没有本地句柄时低频探测一次：覆盖重装/刷新后直接接上正在跑的任务
          const now = Date.now()
          if (now - lastProbe < 3000) return
          lastProbe = now
          const active = await getActiveRelocateJob().catch(() => null)
          if (stopped) return
          if (!active) return
          saveRelocateJobId(active.job_id)
          jobId = active.job_id
        }

        let job
        try {
          job = await getRelocateStorageJob(jobId)
        } catch {
          // 任务已过期（后端保留一小时）：丢掉句柄，下一轮再探测 active
          clearRelocateJobId()
          return
        }
        if (stopped) return

        if (job.phase === 'done') {
          clearRelocateJobId()
          if (isDesktopApp()) {
            setFinishing({ restarting: true })
            window.setTimeout(() => {
              void invoke('restart_app').catch(() => undefined)
            }, 1200)
          } else {
            setFinishing({ restarting: false })
          }
        } else if (job.phase === 'failed') {
          clearRelocateJobId()
          notifyRelocateFailed(job.error)
        }
      } finally {
        polling = false
      }
    }

    const timer = window.setInterval(() => void tick(), 900)
    void tick()
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [])

  if (!finishing) return null
  return (
    <div className="storage-restart-toast" role="status">
      {finishing.restarting
        ? '文献库迁移完成，正在重启应用…'
        : '文献库迁移完成，请重启应用以使用新位置。'}
    </div>
  )
}