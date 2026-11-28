import { useEffect, useRef, useState } from 'react'
import { IconChevron, IconList } from '../components/icons'
import { Markdown } from '../components/Markdown'
import {
  formatDuration,
  type ChatTurn,
  type ProcessBlock,
} from '../lib/researchTypes'
import { splitLeadHeading } from './leadHeading'
import { PlanList } from './PlanList'
import { ReasoningPanel } from './ReasoningPanel'
import { ToolCard } from './ToolCard'

/** 一轮助手输出：过程（思考/计划/工具）整体折叠 + 正文 + 失败提示。 */
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
    return '模型服务在回答途中断开、没有正常结束这一轮：本地代理已会补齐结束标记，请重试一次；若每次都停在同一位置，说明该服务地址的流式输出不稳定，建议核对服务地址是否是 API 地址（通常以 /v1 结尾），或换一个模型。'
  }
  if (/Cannot find module|ERR_MODULE_NOT_FOUND|ENOENT|EISDIR|EACCES/i.test(text)) {
    return '研究 agent 缺少运行文件或依赖：请把上面的错误原文反馈给我们。'
  }
  return null
}

/**
 * 旧快照没有 blocks 时按「思考 → 计划 → 工具」回退拼接。
 * 新事件按真实 step 顺序写入 blocks，因此只有历史缓存会走这条路径。
 */
function processBlocks(turn: ChatTurn): ProcessBlock[] {
  const blocks = turn.blocks ?? []
  if (blocks.length > 0) return blocks
  const fallback: ProcessBlock[] = []
  if (turn.reasoning) {
    fallback.push({
      kind: 'reasoning',
      id: 'legacy-reasoning',
      text: turn.reasoning,
      at: turn.startedAt,
    })
  }
  if (turn.plan.length > 0) {
    fallback.push({ kind: 'plan', id: 'legacy-plan', at: turn.startedAt })
  }
  for (const tool of turn.tools) {
    fallback.push({
      kind: 'tool',
      id: `legacy-${tool.key}`,
      callId: tool.callId || tool.key,
      at: tool.startedAt ?? turn.startedAt,
    })
  }
  return fallback
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
  const lead = splitLeadHeading(turn.text)
  const body = lead ? lead.body : turn.text
  const blocks = processBlocks(turn)
  // 计划是任务进度，不随「过程」一起折叠，始终可见。
  const planBlock = blocks.find((block) => block.kind === 'plan')
  const processOnly = blocks.filter((block) => block.kind !== 'plan')
  const empty = blocks.length === 0 && !body.trim()
  const emptyReply = !running && !cancelled && !interrupted && !failed && empty
  const lastReasoning = [...processOnly]
    .reverse()
    .find((block) => block.kind === 'reasoning')

  // 过程区：运行中展开，轮次结束（进入下一个任务）时自动收起。
  const [processOpen, setProcessOpen] = useState(running)
  const wasRunning = useRef(running)
  useEffect(() => {
    if (wasRunning.current && !running) setProcessOpen(false)
    else if (!wasRunning.current && running) setProcessOpen(true)
    wasRunning.current = running
  }, [running])

  const toolCount = turn.tools.length
  const countText = toolCount > 0 ? `${toolCount} 次工具调用` : `${blocks.length} 步`
  const stateText = running
    ? '进行中'
    : cancelled
      ? '已停止'
      : interrupted
        ? '已中断'
        : failed
          ? '失败'
          : `用时 ${duration}`

  return (
    <article className="turn-block" data-running={running} data-failed={failed}>
      {planBlock && <PlanList key={planBlock.id} items={turn.plan} />}
      {processOnly.length > 0 && (
        <section
          className="process-group"
          data-open={processOpen}
          data-running={running}
          data-failed={failed || undefined}
        >
          <button
            type="button"
            className="process-head"
            aria-expanded={processOpen}
            onClick={() => setProcessOpen((value) => !value)}
          >
            <span className="process-leading" aria-hidden="true">
              <IconList />
            </span>
            <span className="process-title">执行过程</span>
            <span className="process-sep" aria-hidden="true" />
            <span className="process-summary">
              {countText} · {stateText}
            </span>
            <span className="process-chevron" data-open={processOpen}>
              <IconChevron />
            </span>
          </button>
          {processOpen && (
            <div className="process-body">
              {processOnly.map((block) => {
                if (block.kind === 'reasoning') {
                  // 只有最后一段思考且还没有正文时，才显示为“正在思考”。
                  const live = running && !body.trim() && block.id === lastReasoning?.id
                  return (
                    <ReasoningPanel key={block.id} text={block.text} running={live} />
                  )
                }

                const tool = turn.tools.find(
                  (item) => (item.callId || item.key) === block.callId,
                )
                return tool ? <ToolCard key={block.id} tool={tool} /> : null
              })}
            </div>
          )}
        </section>
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
      {!running && blocks.length === 0 && (cancelled || interrupted || failed) && (
        <footer className="turn-foot">
          <span>{cancelled ? '已停止' : interrupted ? '已中断' : '失败'}</span>
          <em>· 用时 {duration}</em>
        </footer>
      )}
    </article>
  )
}