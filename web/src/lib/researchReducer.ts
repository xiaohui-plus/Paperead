/** 把会话事件归约成可渲染的转写：按 turn / callId 合并，按 seq 去重。 */

import {
  EMPTY_CHAT,
  asText,
  type AgentEvent,
  type ChatState,
  type ChatTurn,
  type PermissionPreset,
  type ToolCall,
} from './researchTypes'

const PERMISSION_PRESETS: PermissionPreset[] = [
  'read-only',
  'workspace-write',
  'danger-full-access',
]

function blankTurn(turn: number, at: number): ChatTurn {
  return {
    turn,
    startedAt: at,
    endedAt: null,
    reason: null,
    text: '',
    reasoning: '',
    tools: [],
    plan: [],
  }
}

function turnIndex(state: ChatState, turn: number): number {
  return state.turns.findIndex((item) => item.turn === turn)
}

function turnReason(value: unknown): { kind: string; text: string } {
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    const kind = typeof record.kind === 'string' ? record.kind : ''
    const failure = record.error
    if (failure && typeof failure === 'object') {
      const detail = failure as Record<string, unknown>
      const code = typeof detail.code === 'string' ? detail.code : ''
      const message = asText(failure)
      return { kind, text: [code, message].filter(Boolean).join('：') }
    }
    return { kind, text: asText(failure) }
  }
  return { kind: asText(value), text: '' }
}

function withTurn(state: ChatState, turn: number, at: number): [ChatState, number] {
  const index = turnIndex(state, turn)
  if (index >= 0) return [state, index]
  const turns = [...state.turns, blankTurn(turn, at)]
  return [{ ...state, turns }, turns.length - 1]
}

function patchTurn(
  state: ChatState,
  index: number,
  patch: (turn: ChatTurn) => ChatTurn,
): ChatState {
  const turns = state.turns.map((item, i) => (i === index ? patch(item) : item))
  return { ...state, turns }
}

function lastTurnIndex(state: ChatState): number {
  return state.turns.length - 1
}

function matchTool(turn: ChatTurn, callId?: string): number {
  if (callId) return turn.tools.findIndex((tool) => tool.callId === callId)
  for (let i = turn.tools.length - 1; i >= 0; i -= 1) {
    if (turn.tools[i].status === 'running' || turn.tools[i].status === 'draft') {
      return i
    }
  }
  return -1
}

function upsertTool(
  turn: ChatTurn,
  callId: string | undefined,
  at: number,
  patch: (tool: ToolCall) => ToolCall,
): ChatTurn {
  const index = matchTool(turn, callId)
  if (index >= 0) {
    const tools = turn.tools.map((tool, i) => (i === index ? patch(tool) : tool))
    return { ...turn, tools }
  }
  const created: ToolCall = {
    key: callId || `tool-${turn.tools.length}-${at}`,
    callId: callId || '',
    name: '',
    status: 'draft',
    argumentsText: '',
    startedAt: at,
    endedAt: null,
  }
  return { ...turn, tools: [...turn.tools, patch(created)] }
}

export function applyEvent(state: ChatState, event: AgentEvent): ChatState {
  if (event.seq > 0 && event.seq <= state.lastSeq) return state
  const base: ChatState = event.seq > 0 ? { ...state, lastSeq: event.seq } : state
  const at = event.at || Date.now()

  switch (event.type) {
    case 'message.user': {
      const text = asText(event.text)
      const mode = typeof event.mode === 'string' ? event.mode : undefined
      const scope = typeof event.scope === 'string' ? event.scope : undefined
      const last = base.messages[base.messages.length - 1]
      if (last?.local && last.text === text) {
        const messages = base.messages.slice(0, -1)
        return {
          ...base,
          messages: [...messages, { ...last, local: false, at, mode, scope }],
        }
      }
      return {
        ...base,
        messages: [
          ...base.messages,
          { id: `m-${event.seq || at}`, at, text, mode, scope },
        ],
      }
    }

    case 'command.result': {
      const name = asText(event.name)
      // 权限切换由输入框底部控件直接呈现，不再额外插一条
      // “/permission · preset …” 的运行日志。
      if (name === 'permission') return base
      const text = asText(event.text)
      return {
        ...base,
        notices: [
          ...base.notices,
          {
            id: `c-${event.seq || at}`,
            at,
            level: event.kind === 'error' ? 'error' : 'info',
            text: `/${name}${text ? ` · ${text}` : ' 已执行'}`,
          },
        ],
      }
    }

    case 'turn.start': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return {
        ...patchTurn(next, index, (turn) => ({
          ...turn,
          startedAt: turn.startedAt || at,
          endedAt: null,
        })),
        running: true,
        offline: false,
      }
    }

    case 'turn.end': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      const ended = turnReason(event.reason)
      return {
        ...patchTurn(next, index, (turn) => ({
          ...turn,
          endedAt: at,
          reason: ended.kind || 'done',
          errorText: ended.text || undefined,
        })),
        running: false,
      }
    }

    case 'text.delta': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) => ({
        ...turn,
        text: turn.text + asText(event.text),
      }))
    }

    case 'reasoning.delta': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) => ({
        ...turn,
        reasoning: turn.reasoning + asText(event.text),
      }))
    }

    case 'tool.draft': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) =>
        upsertTool(turn, event.callId, at, (tool) => ({
          ...tool,
          name: asText(event.name) || tool.name,
          status: tool.status === 'running' ? 'running' : 'draft',
          argumentsText: tool.argumentsText + asText(event.argumentsDelta),
        })),
      )
    }

    case 'tool.start': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) =>
        upsertTool(turn, event.callId, at, (tool) => ({
          ...tool,
          name: asText(event.name) || tool.name,
          status: 'running',
          input: event.input ?? tool.input,
          meta: event.meta ?? tool.meta,
          startedAt: tool.startedAt ?? at,
        })),
      )
    }

    case 'tool.end': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) =>
        upsertTool(turn, event.callId, at, (tool) => ({
          ...tool,
          name: asText(event.name) || tool.name,
          status: event.isError ? 'error' : 'done',
          error: asText(event.error) || tool.error,
          result: asText(event.result) || tool.result,
          meta: event.meta ?? tool.meta,
          endedAt: at,
        })),
      )
    }

    case 'plan.update': {
      const items = Array.isArray(event.items) ? event.items : []
      const index = lastTurnIndex(base)
      if (index < 0) return base
      return patchTurn(base, index, (turn) => ({ ...turn, plan: items }))
    }

    case 'approval.request': {
      const requestId = asText(event.requestId)
      if (!requestId || base.approvals.some((item) => item.requestId === requestId)) {
        return base
      }
      return {
        ...base,
        approvals: [
          ...base.approvals,
          {
            requestId,
            toolName: asText(event.toolName),
            callId: typeof event.callId === 'string' ? event.callId : undefined,
            reason: asText(event.reason) || undefined,
            at,
          },
        ],
      }
    }

    case 'approval.resolved': {
      const requestId = asText(event.requestId)
      return {
        ...base,
        approvals: base.approvals.filter((item) => item.requestId !== requestId),
      }
    }

    case 'permission.update': {
      const preset = asText(event.preset) as PermissionPreset
      if (!PERMISSION_PRESETS.includes(preset)) return base
      return { ...base, permission: preset }
    }

    case 'title.update':
      return { ...base, title: asText(event.title) || base.title }

    case 'notice': {
      // 离线由顶部 banner 呈现，不再重复塞进转写。
      if (event.code === 'agent.offline') {
        return { ...base, offline: true }
      }
      return {
        ...base,
        notices: [
          ...base.notices,
          {
            id: `n-${event.seq || at}`,
            at,
            level: asText(event.level) || 'info',
            text: asText(event.text),
          },
        ],
      }
    }

    default:
      return base
  }
}

export function applyEvents(state: ChatState, events: AgentEvent[]): ChatState {
  let next = state
  for (const event of events) next = applyEvent(next, event)
  return next
}

/**
 * 历史快照的批量归约：把连续的增量事件先合并再归约。
 *
 * 长会话里 tool.draft / text.delta 动辄上万条，逐条 upsert 会让参数字符串
 * 反复拼接、每批都复制工具数组，切换会话时要等十几秒。合并后每条工具/
 * 每段文本只归约一次，既快又不改变最终显示结果。
 */
export function applyHistoryEvents(
  state: ChatState,
  events: AgentEvent[],
): ChatState {
  let next = state
  let index = 0
  while (index < events.length) {
    const event = events[index]
    const turn = event.turn ?? 1
    if (event.type === 'text.delta' || event.type === 'reasoning.delta') {
      const parts = [asText(event.text)]
      let last = event
      let cursor = index + 1
      while (
        cursor < events.length &&
        events[cursor].type === event.type &&
        (events[cursor].turn ?? 1) === turn
      ) {
        last = events[cursor]
        parts.push(asText(last.text))
        cursor += 1
      }
      next = applyEvent(next, {
        ...event,
        text: parts.join(''),
        seq: last.seq,
        at: last.at || event.at,
      })
      index = cursor
      continue
    }
    if (event.type === 'tool.draft' && event.callId) {
      const parts = [asText(event.argumentsDelta)]
      let last = event
      let cursor = index + 1
      while (
        cursor < events.length &&
        events[cursor].type === 'tool.draft' &&
        (events[cursor].turn ?? 1) === turn &&
        events[cursor].callId === event.callId
      ) {
        last = events[cursor]
        parts.push(asText(last.argumentsDelta))
        cursor += 1
      }
      next = applyEvent(next, {
        ...event,
        argumentsDelta: parts.join(''),
        seq: last.seq,
        at: last.at || event.at,
      })
      index = cursor
      continue
    }
    next = applyEvent(next, event)
    index += 1
  }
  return next
}

/**
 * 历史回放结束时的状态收敛。
 *
 * 历史事件里可能有未配对的 turn.start / tool.start（agent 断流、进程重启），
 * 直接按实时事件渲染会让旧轮次永远停在“正在思考/运行中”。这里把历史中
 * 已经不可能再更新的轮次与工具标记为结束；只有会话确实还在跑时，才保留
 * 最后一个未结束的轮次继续显示运行态。
 */
export function finalizeHistory(state: ChatState, active: boolean): ChatState {
  if (state.turns.length === 0) {
    return { ...state, running: false }
  }
  const lastIndex = state.turns.length - 1
  const last = state.turns[lastIndex]
  const stillRunning = active && last.endedAt === null
  const turns = state.turns.map((turn, index) => {
    const current = stillRunning && index === lastIndex
    let keepTool = -1
    if (current) {
      for (let i = turn.tools.length - 1; i >= 0; i -= 1) {
        const status = turn.tools[i].status
        if (status === 'running' || status === 'draft') {
          keepTool = i
          break
        }
      }
    }
    const settledAt = turn.tools.reduce(
      (latest, tool) => Math.max(latest, tool.endedAt ?? 0),
      turn.startedAt,
    )
    const tools = turn.tools.map((tool, i) => {
      if (tool.status !== 'running' && tool.status !== 'draft') return tool
      if (current && i === keepTool) return tool
      return { ...tool, status: 'done' as const, endedAt: tool.endedAt ?? settledAt }
    })
    if (current) {
      return tools === turn.tools ? turn : { ...turn, tools }
    }
    const endedAt = turn.endedAt ?? settledAt
    return {
      ...turn,
      tools,
      endedAt,
      reason: turn.reason ?? 'interrupted',
    }
  })
  return { ...state, turns, running: stillRunning }
}

/** 发送前的乐观消息：等 message.user 事件到达时原位替换。 */
export function addLocalMessage(
  state: ChatState,
  text: string,
  meta?: { mode?: string; scope?: string },
): ChatState {
  return {
    ...state,
    messages: [
      ...state.messages,
      {
        id: `local-${Date.now()}`,
        at: Date.now(),
        text,
        local: true,
        mode: meta?.mode,
        scope: meta?.scope,
      },
    ],
  }
}

export { EMPTY_CHAT }
