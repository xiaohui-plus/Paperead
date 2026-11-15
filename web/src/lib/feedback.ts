export interface FeedbackStatus {
  configured: boolean
  source: 'user' | 'env' | 'builtin' | 'none'
  owner: string
  repo: string
  issue_page: string
  max_images: number
  max_image_bytes: number
}

export interface FeedbackImagePayload {
  name?: string
  type: string
  data: string
}

export interface FeedbackPayload {
  title: string
  body: string
  images: FeedbackImagePayload[]
}

export interface FeedbackResult {
  issue_url: string
  number: string
  images: number
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail
    throw new Error(typeof detail === 'string' && detail ? detail : `HTTP ${res.status}`)
  }
  return data as T
}

export function getFeedbackStatus(): Promise<FeedbackStatus> {
  return json<FeedbackStatus>('/api/feedback/status')
}

export function submitFeedback(payload: FeedbackPayload): Promise<FeedbackResult> {
  return json<FeedbackResult>('/api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function saveFeedbackToken(token: string): Promise<FeedbackStatus> {
  return json<FeedbackStatus>('/api/feedback/token', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  })
}

export function clearFeedbackToken(): Promise<FeedbackStatus> {
  return json<FeedbackStatus>('/api/feedback/token', { method: 'DELETE' })
}