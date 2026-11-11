import { IconCheck } from '../components/icons'
import { Markdown } from '../components/Markdown'
import {
  asText,
  formatDuration,
  type ChatTurn,
  type PlanItem,
} from '../lib/researchTypes'
import { splitLeadHeading } from './leadHeading'
import { ReasoningPanel } from './ReasoningPanel'
import { ToolGroup, groupTools } from './ToolGroup'

function planLabel(item: PlanItem, index: number): string {
  return (
    asText(item.content) ||
    asText(item.title) ||
    asText(item.text) ||
    (typeof item.step === 'string' ? item.step : '') ||
    `步骤 ${index + 1}`
  )
}

function PlanList({ items }: { items: PlanItem[] }) {
  return (
    <ul className="plan-list">
      {items.map((item, index) => (
        <li key={String(item.id ?? index)} data-status={String(item.status || '')}>
          <span className="plan-mark">
            {item.status === 'completed' ? <IconCheck /> : null}
          </span>
          <span>{planLabel(item, index)}</span>
        </li>
      ))}
    </ul>
  )
}

/** 一轮助手输出：思考行 + 计划 + 工具行 + 正文 + 弱化收尾。 */
export function TurnBlock({ turn, now }: { turn: ChatTurn; now: number }) {
  const running = !turn.endedAt
  const duration = formatDuration((turn.endedAt ?? now) - turn.startedAt)
  const cancelled = turn.reason === 'cancelled' || turn.reason === 'aborted'
  const groups = groupTools(turn.tools)
  const lead = splitLeadHeading(turn.text)
  const body = lead ? lead.body : turn.text
  const empty = !turn.reasoning && !body.trim() && groups.length === 0

  return (
    <article className="turn-block" data-running={running}>
      {turn.reasoning && (
        <ReasoningPanel
          text={turn.reasoning}
          running={running}
          duration={running ? undefined : duration}
        />
      )}
      {turn.plan.length > 0 && <PlanList items={turn.plan} />}
      {groups.length > 0 && (
        <div className="turn-tools">
          {groups.map((group, index) => (
            <ToolGroup key={`${group.name}-${index}`} group={group} />
          ))}
        </div>
      )}
      {body.trim() && (
        <div className="turn-text">
          <Markdown text={body} />
        </div>
      )}
      {running && empty && <p className="turn-pending">正在准备下一步…</p>}
      {!running && (cancelled || !turn.reasoning) && (
        <footer className="turn-foot">
          <span>{cancelled ? '已停止' : '已完成'}</span>
          {!turn.reasoning && <em>· 用时 {duration}</em>}
          {turn.tools.length > 0 && (
            <em>· {turn.tools.length} 次工具调用</em>
          )}
        </footer>
      )}
    </article>
  )
}
