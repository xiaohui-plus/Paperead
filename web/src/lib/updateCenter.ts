/**
 * 后台更新中心：桌面壳在启动后静默检查并下载新版本，这里保存它的状态，
 * 让任意页面都能在侧栏显示「已下载，可安装」入口。
 */

import { isDesktopApp, watchUpdateProgress } from './update'

export type UpdateCenterStage =
  | 'idle'
  | 'checking'
  | 'downloading'
  | 'ready'
  | 'installing'
  | 'launching'
  | 'up-to-date'
  | 'failed'

export interface UpdateCenterState {
  stage: UpdateCenterStage
  version: string
  percent: number
  message: string
}

interface NativeUpdateStatus {
  stage?: string
  version?: string
  percent?: number
  message?: string
}

const STAGES: UpdateCenterStage[] = [
  'idle',
  'checking',
  'downloading',
  'ready',
  'installing',
  'launching',
  'up-to-date',
  'failed',
]

const INITIAL: UpdateCenterState = {
  stage: 'idle',
  version: '',
  percent: 0,
  message: '',
}

let state: UpdateCenterState = INITIAL
let started = false
const listeners = new Set<() => void>()

function emit(next: UpdateCenterState) {
  state = next
  for (const listener of listeners) listener()
}

function tauriInvoke():
  | ((cmd: string, args?: Record<string, unknown>) => Promise<unknown>)
  | undefined {
  if (typeof window === 'undefined') return undefined
  return (window as any).__TAURI_INTERNALS__?.invoke
}

export function getUpdateCenterState(): UpdateCenterState {
  return state
}

export function subscribeUpdateCenter(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function applyNative(raw: NativeUpdateStatus | null | undefined) {
  const stage = String(raw?.stage || '')
  if (!STAGES.includes(stage as UpdateCenterStage)) return
  // 一次检查/安装失败不能抹掉已经下载好的安装包，入口留着让用户重试。
  if (stage === 'failed' && (state.stage === 'ready' || state.stage === 'installing')) return
  emit({
    stage: stage as UpdateCenterStage,
    version: String(raw?.version || '') || state.version,
    percent: Number(raw?.percent) || 0,
    message: String(raw?.message || ''),
  })
}

/** 点击更新入口时先给即时反馈，真实进度事件随后覆盖。 */
export function markUpdateInstalling() {
  if (state.stage !== 'ready') return
  emit({ ...state, stage: 'installing', percent: 0, message: '正在安装…' })
}

/** 安装失败时保留入口（可重试），把原因放进按钮提示。 */
export function reportUpdateInstallError(message: string) {
  if (state.stage !== 'ready' && state.stage !== 'installing') return
  emit({ ...state, stage: 'ready', message: message || '安装失败，点击重试' })
}

/** 桌面端调用一次：读当前后台下载状态，并订阅后续进度事件。 */
export function startUpdateCenter() {
  if (started || !isDesktopApp()) return
  started = true

  const invoke = tauriInvoke()
  if (invoke) {
    void invoke('get_update_status')
      .then((raw) => applyNative(raw as NativeUpdateStatus))
      .catch(() => {})
  }
  void watchUpdateProgress((progress) => applyNative(progress))
}