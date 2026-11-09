/** 把会话事件归约成可渲染的转写：按 turn / callId 合并，按 seq 去重。 */

import {
  EMPTY_CHAT,
  type AgentEvent,
  type ChatState,
  type ChatTurn,
  type ToolCall,
} from './researchTypes'

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
      const text = event.text || ''
      const last = base.messages[base.messages.length - 1]
      if (last?.local && last.text === text) {
        const messages = base.messages.slice(0, -1)
        return { ...base, messages: [...messages, { ...last, local: false, at }] }
      }
      return {
        ...base,
        messages: [
          ...base.messages,
          { id: `m-${event.seq || at}`, at, text },
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
      return {
        ...patchTurn(next, index, (turn) => ({
          ...turn,
          endedAt: at,
          reason: event.reason || 'done',
        })),
        running: false,
      }
    }

    case 'text.delta': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) => ({
        ...turn,
        text: turn.text + (event.text || ''),
      }))
    }

    case 'reasoning.delta': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) => ({
        ...turn,
        reasoning: turn.reasoning + (event.text || ''),
      }))
    }

    case 'tool.draft': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) =>
        upsertTool(turn, event.callId, at, (tool) => ({
          ...tool,
          name: event.name || tool.name,
          status: tool.status === 'running' ? 'running' : 'draft',
          argumentsText: tool.argumentsText + (event.argumentsDelta || ''),
        })),
      )
    }

    case 'tool.start': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) =>
        upsertTool(turn, event.callId, at, (tool) => ({
          ...tool,
          name: event.name || tool.name,
          status: 'running',
          input: event.input ?? tool.input,
          startedAt: tool.startedAt ?? at,
        })),
      )
    }

    case 'tool.end': {
      const [next, index] = withTurn(base, event.turn ?? 1, at)
      return patchTurn(next, index, (turn) =>
        upsertTool(turn, event.callId, at, (tool) => ({
          ...tool,
          name: event.name || tool.name,
          status: event.isError ? 'error' : 'done',
          error: event.error || tool.error,
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

    case 'title.update':
      return { ...base, title: event.title || base.title }

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
            level: event.level || 'info',
            text: event.text || '',
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

/** 发送前的乐观消息：等 message.user 事件到达时原位替换。 */
export function addLocalMessage(state: ChatState, text: string): ChatState {
  return {
    ...state,
    messages: [
      ...state.messages,
      { id: `local-${Date.now()}`, at: Date.now(), text, local: true },
    ],
  }
}

export { EMPTY_CHAT }
