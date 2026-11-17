export interface PaperMetadata {
  source: string
  source_id: string
  title: string
  authors: string
  abstract: string
  doi: string
  url: string
  year: number | null
  venue: string
  pdf_url: string
}

export interface PaperSummary {
  id: string
  title: string
  status: string
  open_url: string
  [key: string]: unknown
}

export interface TaskState {
  id: string
  tabId: number
  title: string
  status: 'working' | 'ready' | 'failed' | 'existing'
  message: string
  paperId?: string
  openUrl?: string
}
