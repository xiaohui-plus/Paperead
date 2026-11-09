import { IconCheck, IconSpark } from '../components/icons'
import { Markdown } from '../components/Markdown'
import {
  formatDuration,
  type ChatTurn,
  type PlanItem,
} from '../lib/researchTypes'
import { ReasoningPanel } from './ReasoningPanel'
import { ToolGroup, groupTools } from './ToolGroup'

function planLabel(item: PlanItem, index: number): string {
  return (
    item.content ||
    item.title ||
    item.text ||
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

/** 一轮助手输出：状态头 + 思考 + 计划 + 工具卡 + 正文。 */
export function TurnBlock({ turn, now }: { turn: ChatTurn; now: number }) {
  const running = !turn.endedAt
  const duration = formatDuration((turn.endedAt ?? now) - turn.startedAt)
  const cancelled = turn.reason === 'cancelled' || turn.reason === 'aborted'
  const status = running ? '正在研究' : cancelled ? '已停止' : '已完成'
  const groups = groupTools(turn.tools)

  return (
    <article className="turn-block">
      <header className="turn-head">
        <span className="turn-avatar">
          <IconSpark />
        </span>
        <strong>{status}</strong>
        <em>· {duration}</em>
        {turn.tools.length > 0 && <em>· {turn.tools.length} 次工具调用</em>}
      </header>

      {turn.reasoning && <ReasoningPanel text={turn.reasoning} running={running} />}
      {turn.plan.length > 0 && <PlanList items={turn.plan} />}
      {groups.length > 0 && (
        <div className="turn-tools">
          {groups.map((group, index) => (
            <ToolGroup key={`${group.name}-${index}`} group={group} />
          ))}
        </div>
      )}
      {turn.text && (
        <div className="turn-text">
          <Markdown text={turn.text} />
        </div>
      )}
      {running && !turn.text && groups.length === 0 && (
        <p className="turn-pending">正在准备下一步…</p>
      )}
    </article>
  )
}
