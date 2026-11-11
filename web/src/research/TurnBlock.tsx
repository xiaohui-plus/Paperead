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
const FAILED_REASONS = new Set(['error', 'blocked', 'max-tokens'])

function failureHint(text?: string): string | null {
  if (!text) return null
  if (/MISSING_CREDENTIAL|no API key/i.test(text)) {
    return '研究 agent 没有拿到模型密钥：请到 设置 → 配置模型 重新保存一次回答模型。'
  }
  if (/AUTH|401|invalid api key|unauthorized/i.test(text)) {
    return '模型密钥被拒绝：请检查 API Key 是否正确、是否具备该模型的权限。'
  }
  if (/TRANSPORT|ECONN|ENOTFOUND|certificate|timed? ?out|连接模型服务失败/i.test(text)) {
    return '连不上模型服务：请检查网络、代理，以及模型配置里的 base_url 是否可用。'
  }
  if (/STREAM_CLOSED/i.test(text)) {
    return '模型服务没有按流式返回内容：请检查 设置 → 配置模型 里的服务地址（应填 API 地址，通常以 /v1 结尾）和模型名。'
  }
  if (/Cannot find module|ERR_MODULE_NOT_FOUND|ENOENT|EISDIR|EACCES/i.test(text)) {
    return '研究 agent 缺少运行文件或依赖：请把上面的错误原文反馈给我们。'
  }
  return null
}

export function TurnBlock({ turn, now }: { turn: ChatTurn; now: number }) {
  const running = !turn.endedAt
  const duration = formatDuration((turn.endedAt ?? now) - turn.startedAt)
  const cancelled = turn.reason === 'cancelled' || turn.reason === 'aborted'
  const interrupted = turn.reason === 'interrupted'
  const failed =
    !running &&
    !cancelled &&
    !interrupted &&
    (FAILED_REASONS.has(turn.reason ?? '') || Boolean(turn.errorText))
  const hint = failed || interrupted ? failureHint(turn.errorText) : null
  const groups = groupTools(turn.tools)
  const lead = splitLeadHeading(turn.text)
  const body = lead ? lead.body : turn.text
  const empty = !turn.reasoning && !body.trim() && groups.length === 0
  const emptyReply = !running && !cancelled && !interrupted && !failed && empty

  return (
    <article className="turn-block" data-running={running} data-failed={failed}>
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
      {(failed || interrupted) && turn.errorText && (
        <p className="turn-error">{turn.errorText}</p>
      )}
      {hint && <p className="turn-hint">{hint}</p>}
      {emptyReply && (
        <p className="turn-empty">
          模型这一轮没有返回任何内容。可在设置里确认回答模型，或换个问法重试。
        </p>
      )}
      {!running && (cancelled || interrupted || failed || !turn.reasoning) && (
        <footer className="turn-foot">
          <span>
            {cancelled ? '已停止' : interrupted ? '已中断' : failed ? '失败' : '已完成'}
          </span>
          {!turn.reasoning && <em>· 用时 {duration}</em>}
          {turn.tools.length > 0 && <em>· {turn.tools.length} 次工具调用</em>}
        </footer>
      )}
    </article>
  )
}
