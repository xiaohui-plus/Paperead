import { useCallback, useEffect, useRef, useState } from 'react'
import { useDialogDrag } from '../lib/useDialogDrag'
import {
  getEngineStatus,
  startEngineDownload,
  type EngineDownloadStatus,
} from '../lib/library'
import { IconCheck, IconClose, IconDownload, IconTranslate } from './icons'

const EMPTY: EngineDownloadStatus = {
  status: 'idle',
  phase: '',
  percent: 0,
  bytes: 0,
  total: 0,
  source: '',
  error: '',
  installed: false,
  ready: false,
  version: '',
  home: '',
  startedAt: 0,
  finishedAt: 0,
}

const PHASE: Record<string, string> = {
  manifest: '获取清单',
  download: '下载依赖',
  verify: '校验文件',
  extract: '解压安装',
  done: '完成',
}

function formatSize(bytes?: number): string {
  if (!bytes || bytes <= 0) return ''
  const mb = bytes / (1024 * 1024)
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`
  if (mb >= 1) return `${mb.toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

/**
 * 全局翻译引擎（BabelDOC）下载进度弹窗。
 *
 * 触发场景：点「译文 / 对照」时引擎还没装好。此前只给一行红字报错，
 * 用户不知道它正在下载还是已经失败；这里把后台下载进度、失败原因与
 * 重试入口集中到一个窗口里，下载完成后自动继续翻译。
 */
export function EngineDownloadDialog({
  open,
  onClose,
  onReady,
}: {
  open: boolean
  onClose: () => void
  onReady: () => void
}) {
  const drag = useDialogDrag<HTMLDivElement>()
  const [state, setState] = useState<EngineDownloadStatus>(EMPTY)
  const [failed, setFailed] = useState('')
  const readyRef = useRef(onReady)
  readyRef.current = onReady

  // 打开即接上后台下载：已在下载会直接返回当前进度，未开始则启动。
  useEffect(() => {
    if (!open) return
    let alive = true
    setFailed('')
    void startEngineDownload()
      .then((data) => {
        if (alive) setState(data)
      })
      .catch((e) => {
        if (alive) setFailed(e instanceof Error ? e.message : '启动下载失败')
      })
    const timer = window.setInterval(() => {
      void getEngineStatus()
        .then((data) => {
          if (alive) setState(data)
        })
        .catch(() => {
          /* 网络抖动不打断进度展示 */
        })
    }, 1200)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [open])

  // 就绪后略等一拍再关闭：让用户看到「已完成」，随后自动继续翻译。
  // 旧版本引擎（installed 但非期望版本）同样可用，不能卡在弹窗里。
  useEffect(() => {
    if (!open || !(state.ready || state.installed)) return
    const timer = window.setTimeout(() => {
      readyRef.current()
      onClose()
    }, 700)
    return () => window.clearTimeout(timer)
  }, [open, state.ready, state.installed, onClose])

  const retry = useCallback(() => {
    setFailed('')
    setState((prev) => ({ ...prev, status: 'running', percent: 0, error: '' }))
    void startEngineDownload(true)
      .then(setState)
      .catch((e) => setFailed(e instanceof Error ? e.message : '启动下载失败'))
  }, [])

  if (!open) return null

  const usable = state.ready || state.installed
  const percent = usable ? 100 : Math.max(0, Math.min(100, state.percent))
  const running = state.status === 'running' || state.status === 'idle'
  const broken = state.status === 'done' && !usable
  const failedText =
    failed ||
    (state.status === 'error' ? state.error : '') ||
    (broken ? '依赖已下载，但引擎启动文件校验未通过，请重试重新安装。' : '')
  const phaseText = PHASE[state.phase ?? ''] || '下载依赖'
  const sizeText =
    state.total && state.bytes
      ? `${formatSize(state.bytes)} / ${formatSize(state.total)}`
      : formatSize(state.bytes)

  return (
    <div className="modal-mask engine-dialog-mask">
      <div
        className="modal engine-dialog"
        role="dialog"
        aria-modal
        aria-labelledby="engine-dialog-title"
        ref={drag.ref}
        {...drag.props}
      >
        <div className="engine-dialog-head" data-drag-handle>
          <span className="engine-dialog-icon">
            {usable ? <IconCheck /> : <IconTranslate />}
          </span>
          <div className="engine-dialog-title">
            <h2 id="engine-dialog-title">
              {usable ? '翻译引擎已就绪' : broken ? '翻译依赖安装未通过' : '正在下载翻译依赖'}
            </h2>
            <p>
              {state.source && running
                ? `下载源：${state.source}`
                : '首次使用整篇翻译需要联网下载引擎依赖，之后离线可用。'}
            </p>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <IconClose />
          </button>
        </div>

        <div className="engine-dialog-body">
          <div className="storage-progress">
            <div className="storage-progress-track">
              <span
                className="storage-progress-fill"
                style={{ width: `${Math.max(2, percent)}%` }}
              />
            </div>
            <div className="storage-progress-meta">
              <span>
                {usable
                  ? '已安装'
                  : running
                    ? phaseText
                    : broken
                      ? '安装未通过'
                      : '等待重试'}
                {sizeText && running ? ` · ${sizeText}` : ''}
              </span>
              <span>{usable || broken ? 100 : percent}%</span>
            </div>
          </div>

          {usable ? (
            <p className="engine-dialog-note">下载完成，正在继续翻译…</p>
          ) : running ? (
            <p className="engine-dialog-note">
              {state.error
                ? state.error
                : '下载在后台进行，可以先关掉窗口，完成后回来点「译文 / 对照」即可。'}
            </p>
          ) : null}

          {failedText && (
            <div className="engine-dialog-failed">
              <p>{failedText}</p>
              <button className="btn primary" onClick={retry}>
                <IconDownload /> {broken ? '重新安装' : '重试下载'}
              </button>
            </div>
          )}
        </div>

        <div className="engine-dialog-foot">
          <span>关闭窗口不会中断后台下载。</span>
          <button className="btn small" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  )
}