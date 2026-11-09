export interface OnboardingState {
  prompted: boolean
  promptedAt: number
  choice: 'start' | 'skip' | 'legacy' | null
  status: string
  stepId: string | null
  legacyUser: boolean
  shouldPrompt: boolean
  dismissed: boolean
}

export type OnboardingAction =
  | 'shown'
  | 'start'
  | 'skip'
  | 'legacy'
  | 'progress'
  | 'dismiss'
  | 'complete'

const SHOWN_KEY = 'paperead.tour.shown'
const STEP_KEY = 'paperead.tour.step'

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error((data as any).detail || `HTTP ${response.status}`)
  }
  return data as T
}

export function getOnboarding(): Promise<OnboardingState> {
  return jsonFetch<OnboardingState>('/api/onboarding')
}

export function postOnboarding(
  action: OnboardingAction,
  stepId?: string,
): Promise<OnboardingState> {
  return jsonFetch<OnboardingState>('/api/onboarding', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, stepId }),
  })
}

/** 本地镜像：后端文件写入失败时，避免同一次安装重复弹窗。 */
export function tourPromptShown(): boolean {
  try {
    return localStorage.getItem(SHOWN_KEY) === '1'
  } catch {
    return false
  }
}

export function markTourPromptShown(): void {
  try {
    localStorage.setItem(SHOWN_KEY, '1')
  } catch {
    // 隐私模式或存储被禁用时忽略，后端文件仍是主状态。
  }
}

export function lastTourStep(): string | null {
  try {
    return localStorage.getItem(STEP_KEY)
  } catch {
    return null
  }
}

export function saveLastTourStep(stepId: string | null): void {
  try {
    if (stepId) localStorage.setItem(STEP_KEY, stepId)
    else localStorage.removeItem(STEP_KEY)
  } catch {
    // 忽略存储异常
  }
}
