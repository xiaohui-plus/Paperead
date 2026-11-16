import { useCallback, useEffect, useState } from 'react'
import { open as openDialog } from '@tauri-apps/plugin-dialog'
import {
  getActiveRelocateJob,
  getRelocateStorageJob,
  getStorageInfo,
  RELOCATE_ACTIVE_PHASES,
  startRelocateStorage,
  type StorageInfo,
  type StorageRelocateJob,
} from '../lib/library'
import {
  clearRelocateJobId,
  loadRelocateJobId,
  RELOCATE_FAILED_EVENT,
  saveRelocateJobId,
} from '../lib/storageRelocation'
import { isDesktopApp } from '../lib/update'
import { LocalFolderBrowser } from '../research/LocalFolderBrowser'
import { IconFolder } from './icons'

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败'
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let index = 0
  let size = value
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${index === 0 || size >= 100 ? Math.round(size) : size.toFixed(1)} ${units[index]}`
}

function formatEta(value: number | undefined): string {
  if (!value || !Number.isFinite(value) || value <= 0) return ''
  if (value < 60) return `${Math.ceil(value)} 秒`
  const minutes = Math.floor(value / 60)
  const seconds = Math.round(value % 60)
  return seconds > 0 ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`
}

function percentOf(job: StorageRelocateJob): number {
  if (job.bytes_total > 0) {
    return Math.min(100, Math.max(0, Math.round((job.bytes_done / job.bytes_total) * 100)))
  }
  if (job.files_total > 0) {
    return Math.min(100, Math.max(0, Math.round((job.files_done / job.files_total) * 100)))
  }
  return 0
}

const PHASE_TEXT: Record<StorageRelocateJob['phase'], string> = {
  starting: '正在准备迁移',
  scanning: '正在统计文件',
  copying: '正在复制文件',
  finalizing: '正在写入路径配置',
  done: '迁移完成',
  failed: '迁移失败',
}

/** 设置 → 常规设置：文献库默认保存路径与自动迁移。 */
export function StoragePathPanel() {
  const [info, setInfo] = useState<StorageInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [draft, setDraft] = useState('')
  const [browserOpen, setBrowserOpen] = useState(false)
  const [job, setJob] = useState<StorageRelocateJob | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const busy = Boolean(job && RELOCATE_ACTIVE_PHASES.includes(job.phase))
  const pct = job ? percentOf(job) : 0
  const scanning = job?.phase === 'scanning'
  const speed = job
    ? (job.instant_speed && job.instant_speed > 0 ? job.instant_speed : job.speed)
    : 0

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setInfo(await getStorageInfo())
    } catch (loadError) {
      setError(errorText(loadError))
    } finally {
      setLoading(false)
    }
  }, [])

  const settle = useCallback((next: StorageRelocateJob) => {
    if (next.phase === 'failed') {
      clearRelocateJobId()
      setJob(null)
      setError(next.error || '迁移失败')
      return
    }
    if (next.phase !== 'done' || !next.result) return
    clearRelocateJobId()
    const result = next.result
    setJob(next)
    setInfo({
      path: result.path,
      configured: true,
      configured_path: result.path,
      pending: true,
      fallback: false,
      writable: true,
    })
    setDraft('')
    // 重启由应用根部的迁移守望统一触发，设置页关掉也不会漏
    setNotice(
      `迁移完成（${result.files} 个文件，${formatBytes(result.bytes)}）。应用会自动重启；若没有重启，请手动重启以使用新位置。`,
    )
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // 切换界面/刷新后接上仍在跑的任务：先看本地句柄，再兜底查一次 active。
  useEffect(() => {
    let cancelled = false
    const restore = async () => {
      const saved = loadRelocateJobId()
      if (saved) {
        try {
          const current = await getRelocateStorageJob(saved)
          if (cancelled) return
          if (current.phase === 'done' || current.phase === 'failed') settle(current)
          else setJob(current)
          return
        } catch {
          clearRelocateJobId()
        }
      }
      const active = await getActiveRelocateJob().catch(() => null)
      if (!cancelled && active) {
        saveRelocateJobId(active.job_id)
        setJob(active)
      }
    }
    void restore()
    return () => {
      cancelled = true
    }
  }, [settle])

  // 后台任务：每 400ms 拉一次进度，done/failed 时停止并收尾。
  useEffect(() => {
    const jobId = job?.job_id
    if (!jobId || !job || !RELOCATE_ACTIVE_PHASES.includes(job.phase)) return undefined
    let cancelled = false
    let timer = 0
    let failures = 0
    const tick = async () => {
      try {
        const next = await getRelocateStorageJob(jobId)
        if (cancelled) return
        failures = 0
        if (!RELOCATE_ACTIVE_PHASES.includes(next.phase)) {
          window.clearInterval(timer)
          settle(next)
          return
        }
        setJob(next)
      } catch (pollError) {
        if (cancelled) return
        // 复制大文件时后端可能短暂忙不过来，连续失败 3 次才判定轮询中断。
        failures += 1
        if (failures < 3) return
        window.clearInterval(timer)
        setJob(null)
        setError(errorText(pollError))
      }
    }
    timer = window.setInterval(() => void tick(), 400)
    void tick()
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [job?.job_id, job?.phase, settle])

  // 迁移即使在别的界面失败，也会广播过来；面板打开时直接显示原因。
  useEffect(() => {
    const onFailed = (event: Event) => {
      const message = String((event as CustomEvent).detail || '迁移失败')
      clearRelocateJobId()
      setJob(null)
      setError(message)
    }
    window.addEventListener(RELOCATE_FAILED_EVENT, onFailed)
    return () => window.removeEventListener(RELOCATE_FAILED_EVENT, onFailed)
  }, [])

  const pick = async () => {
    setError('')
    setNotice('')
    if (!isDesktopApp()) {
      setBrowserOpen(true)
      return
    }
    try {
      const picked = await openDialog({
        directory: true,
        multiple: false,
        title: '选择文献库保存位置',
      })
      if (typeof picked === 'string' && picked) setDraft(picked)
    } catch (pickError) {
      setError(errorText(pickError))
    }
  }

  const save = async () => {
    if (!draft || busy) return
    setError('')
    setNotice('')
    try {
      const { status } = await startRelocateStorage(draft)
      saveRelocateJobId(status.job_id)
      setJob(status)
      if (!RELOCATE_ACTIVE_PHASES.includes(status.phase)) settle(status)
    } catch (saveError) {
      setError(errorText(saveError))
    }
  }

  return (
    <section className="storage-path-panel">
      <div className="storage-path-head">
        <div>
          <h3>文献库保存路径</h3>
          <p>PDF、解析结果、研究项目与模型设置都存放在这个文件夹。</p>
        </div>
      </div>

      <div className="storage-path-row">
        <div className="storage-path-value">
          <span>当前位置</span>
          {loading ? (
            <code>读取中…</code>
          ) : (
            <code title={info?.path}>{info?.path || '—'}</code>
          )}
        </div>
        <button className="btn" onClick={() => void pick()} disabled={loading || busy}>
          <IconFolder />
          更改路径
        </button>
      </div>

      {draft && (
        <div className="storage-path-row is-draft">
          <div className="storage-path-value">
            <span>新位置</span>
            <code title={draft}>{draft}</code>
          </div>
          <div className="storage-path-actions">
            <button className="btn ghost" onClick={() => setDraft('')} disabled={busy}>
              取消
            </button>
            <button className="btn primary" onClick={() => void save()} disabled={busy}>
              {busy ? `正在迁移 ${pct}%` : '保存并迁移'}
            </button>
          </div>
        </div>
      )}

      {busy && job && (
        <div className="storage-progress">
          <div className="storage-progress-track">
            <span
              className={`storage-progress-fill${scanning ? ' is-pending' : ''}`}
              style={{ width: `${scanning ? 100 : pct}%` }}
            />
          </div>
          <div className="storage-progress-meta">
            <span>
              {PHASE_TEXT[job.phase]}
              {!scanning ? ` ${pct}%` : ''}
            </span>
            <span>
              {scanning
                ? `已扫描 ${job.files_scanned ?? 0} 个文件 · ${formatBytes(job.bytes_scanned ?? 0)}`
                : job.files_total > 0
                  ? `${job.files_done}/${job.files_total} 个文件 · ${formatBytes(job.bytes_done)} / ${formatBytes(job.bytes_total)}`
                  : formatBytes(job.bytes_done)}
            </span>
          </div>
          {!scanning && (job.current || speed > 0) && (
            <div className="storage-progress-meta is-current">
              <span title={job.current}>{job.current || '—'}</span>
              {speed > 0 && (
                <span>
                  {formatBytes(speed)}/s
                  {job.eta ? ` · 剩余 ${formatEta(job.eta)}` : ''}
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {info?.pending && (
        <p className="storage-path-hint">
          新路径已保存，重启应用后生效；旧目录会在重启后由后台清理。
        </p>
      )}

      {info?.fallback && (
        <p className="storage-path-warn">
          自定义路径 {info.configured_path} 当前不可用，正在使用默认文献库；原路径恢复后重启应用即可继续使用。
        </p>
      )}

      {draft && !busy && (
        <p className="storage-path-hint">
          迁移只复制数据；应用重启并确认新位置可用后，旧目录会在后台清理。
        </p>
      )}
      {busy && (
        <p className="storage-path-hint">
          迁移在后台进行，可以切换界面继续阅读，传输不会中断；复制完成后应用会自动重启。
        </p>
      )}
      {error && <p className="storage-path-error">{error}</p>}
      {notice && <p className="storage-path-notice">{notice}</p>}

      <LocalFolderBrowser
        open={browserOpen}
        title="选择文献库保存位置"
        initialPath={info?.path ?? ''}
        onClose={() => setBrowserOpen(false)}
        onSelect={(path) => {
          setBrowserOpen(false)
          setDraft(path)
        }}
      />
    </section>
  )
}