/** 会话事件与转写模型：与 Python 侧落库的 shape 保持一致。 */

export interface PlanItem {
  id?: string
  content?: string
  title?: string
  text?: string
  status?: string
  [key: string]: unknown
}

export interface AgentEvent {
  seq: number
  at: number
  type: string
  turn?: number
  step?: number
  text?: string
  title?: string
  reason?: string
  level?: 'info' | 'warn' | 'error'
  code?: string
  callId?: string
  name?: string
  argumentsDelta?: string
  input?: unknown
  isError?: boolean
  error?: string
  result?: string
  meta?: unknown
  items?: PlanItem[]
}

export type ToolStatus = 'draft' | 'running' | 'done' | 'error'

export interface ToolCall {
  key: string
  callId: string
  name: string
  status: ToolStatus
  /** tool.draft 阶段拼起来的原始参数，用于提前显示检索式。 */
  argumentsText: string
  input?: unknown
  error?: string
  result?: string
  /** tool.end 透传的展示元数据（如 terminal 的退出码）。 */
  meta?: unknown
  startedAt: number | null
  endedAt: number | null
}

export interface ChatTurn {
  turn: number
  startedAt: number
  endedAt: number | null
  reason: string | null
  text: string
  reasoning: string
  tools: ToolCall[]
  plan: PlanItem[]
}

export interface UserMessage {
  id: string
  at: number
  text: string
  local?: boolean
}

export interface ChatNotice {
  id: string
  at: number
  level: string
  text: string
}

export interface ChatState {
  lastSeq: number
  messages: UserMessage[]
  turns: ChatTurn[]
  notices: ChatNotice[]
  title: string | null
  running: boolean
  offline: boolean
}

export const EMPTY_CHAT: ChatState = {
  lastSeq: 0,
  messages: [],
  turns: [],
  notices: [],
  title: null,
  running: false,
  offline: false,
}

/**
 * 把任意事件字段归一化成可渲染文本。
 * agent 的错误有时是对象（例如 `{name: 'ToolOutputError', code: '...'}`），
 * 直接塞进 React 子节点会触发 error #31 并让整页白屏。
 */
export function asText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(asText).filter(Boolean).join('\n')
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>
    if (typeof record.message === 'string' && record.message.trim()) return record.message
    if (typeof record.error === 'string' && record.error.trim()) return record.error
    const name = typeof record.name === 'string' ? record.name : ''
    const code = typeof record.code === 'string' ? record.code : ''
    if (name || code) return [name, code].filter(Boolean).join(': ')
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
  return String(value)
}

export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分钟`
}

const TOOL_TITLES: Record<string, string> = {
  academic_search: '学术搜索',
  library_search: '文献库',
  paper_read: '精读论文',
  paper_get: '论文详情',
  paper_import: '导入文献',
  paper_ingest: '导入文献',
  project_read: '读取项目',
  project_write: '写入项目',
  artifact_read: '读取产物',
  file_read: '读取文件',
  citation_check: '引用审计',
  run_command: '运行命令',
  library_tag: '文献标签',
  web_search: '网页搜索',
  echo: '回声',
}

export function toolTitle(name: string): string {
  return TOOL_TITLES[name] || name || '工具调用'
}

/** 工具副标题：优先检索式 / DOI / 文件名，其次截断的 JSON。 */
export function toolSubtitle(tool: ToolCall): string {
  const input = tool.input
  if (input && typeof input === 'object') {
    const record = input as Record<string, unknown>
    for (const key of ['query', 'paperId', 'artifactId', 'doi', 'title', 'filename', 'path', 'command', 'source', 'name', 'url']) {
      const value = record[key]
      if (typeof value === 'string' && value.trim()) return value.trim()
    }
  }
  if (typeof input === 'string' && input.trim()) return input.trim().slice(0, 120)
  if (tool.argumentsText) return tool.argumentsText.slice(0, 120)
  return ''
}
