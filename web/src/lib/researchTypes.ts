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
  /** command.result 的结果类型：success / error。 */
  kind?: string
  turn?: number
  step?: number
  text?: string
  title?: string
  reason?: unknown
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
  refs?: unknown[]
  mode?: string
  scope?: string
  line?: string
  /** replay.end：历史回放结束时的最后一条 seq。 */
  lastSeq?: number
  /** replay.end：该会话此刻是否仍有正在运行的轮次。 */
  running?: boolean
  /** 权限申请：agent 侧一次待回答的授权请求。 */
  requestId?: string
  toolName?: string
  preset?: string
  outcome?: string
  /** llm.retry：provider 与重试策略。 */
  provider?: string
  retry?: number
  maxRetries?: number
  delayMs?: number
  failure?: { code?: string; message?: string }
  /** goal.change：目标快照。 */
  operation?: string
  roundsStarted?: number
  goal?: GoalState | null
}

/** 与 dsh 的权限预设一一对应：沙箱模式 + 审批策略。 */
export type PermissionPreset = 'read-only' | 'workspace-write' | 'danger-full-access'

export interface PendingApproval {
  requestId: string
  toolName: string
  callId?: string
  reason?: string
  at: number
}

export type ToolStatus = 'draft' | 'running' | 'done' | 'error'

/**
 * 一轮内的过程片段：保留“思考段 → 工具 → 再思考”的真实先后顺序。
 * 正文不进入过程片段，仍单独渲染在轮次末尾。
 */
export type ProcessBlock =
  | {
      kind: 'reasoning'
      id: string
      step?: number
      text: string
      at: number
    }
  | {
      kind: 'tool'
      id: string
      callId: string
      step?: number
      at: number
    }
  | {
      kind: 'plan'
      id: string
      at: number
    }

export interface ToolCall {
  key: string
  callId: string
  name: string
  status: ToolStatus
  /** 该工具属于模型第几步，用于和思考段对齐。 */
  step?: number
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
  errorText?: string
  text: string
  reasoning: string
  tools: ToolCall[]
  plan: PlanItem[]
  /** 思考/工具/计划的时间线；旧快照没有该字段时由渲染层回退拼接。 */
  blocks?: ProcessBlock[]
}

export interface UserMessage {
  id: string
  at: number
  text: string
  local?: boolean
  mode?: string
  scope?: string
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
  /** 当前会话的权限预设；旧会话没有该事件时为 null。 */
  permission: PermissionPreset | null
  /** 正在等待用户回答的授权请求。 */
  approvals: PendingApproval[]
  /** 模型请求失败后的重试调度（DSH 的 llm/retry，非 surface 事件）。 */
  retry?: RetryState | null
  /** 长程目标状态（DSH 的 goal/change，非 surface 事件）。 */
  goal?: GoalState | null
}

export interface RetryState {
  turn?: number
  step?: number
  retry: number
  maxRetries?: number
  mode?: string
  delayMs: number
  started?: boolean
  failure?: { code?: string; message?: string }
  at: number
}

export interface GoalState {
  id: string
  revision: number
  objective: string
  phase: string
  roundsStarted: number
  maxGoalRounds: number
  blockedReason?: string
}

export const EMPTY_CHAT: ChatState = {
  lastSeq: 0,
  messages: [],
  turns: [],
  notices: [],
  title: null,
  running: false,
  offline: false,
  permission: null,
  approvals: [],
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
  project_update: '更新项目',
  project_write: '写入项目',
  artifact_write: '保存产物',
  artifact_read: '读取产物',
  file_read: '读取文件',
  citation_check: '引用审计',
  run_command: '运行命令',
  library_tag: '文献标签',
  web_search: '网页搜索',
  web_fetch: '网页抓取',
  echo: '回声',
  read: '读取文件',
  write: '写入文件',
  edit: '编辑文件',
  glob: '查找文件',
  grep: '搜索内容',
  pwsh: 'PowerShell',
  bash: '终端',
  job_output: '后台输出',
  job_list: '后台任务',
  job_kill: '停止任务',
  subagent: '子代理',
  subagent_fork: '子代理（继承上下文）',
  send_message: '子代理消息',
  list_agents: '子代理列表',
  report: '子代理回报',
  todo_write: '任务计划',
  skill: '技能',
}

export function toolTitle(name: string): string {
  return TOOL_TITLES[name] || name || '工具调用'
}

/** 工具副标题：优先检索式 / DOI / 文件名，其次截断的 JSON。 */
export function toolSubtitle(tool: ToolCall): string {
  const input = tool.input
  if (input && typeof input === 'object') {
    const record = input as Record<string, unknown>
    for (const key of [
      'query',
      'paperId',
      'artifactId',
      'doi',
      'title',
      'filename',
      'file_path',
      'path',
      'command',
      'prompt',
      'description',
      'subagentId',
      'agentId',
      'jobId',
      'source',
      'name',
      'url',
    ]) {
      const value = record[key]
      if (typeof value === 'string' && value.trim()) return value.trim()
    }
  }
  if (typeof input === 'string' && input.trim()) return input.trim().slice(0, 120)
  if (tool.argumentsText) return tool.argumentsText.slice(0, 120)
  return ''
}
