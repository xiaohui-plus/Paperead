import { IconChevron } from '../components/icons'
import { toolTitle, type ToolCall } from '../lib/researchTypes'
import { ToolCard } from './ToolCard'

export interface ToolGroupData {
  name: string
  tools: ToolCall[]
}

/** 连续同名工具合并成一组，避免 5 张散卡。 */
export function groupTools(tools: ToolCall[]): ToolGroupData[] {
  const groups: ToolGroupData[] = []
  for (const tool of tools) {
    const last = groups[groups.length - 1]
    if (last && last.name === tool.name) last.tools.push(tool)
    else groups.push({ name: tool.name, tools: [tool] })
  }
  return groups
}

function groupStatus(tools: ToolCall[]): string {
  if (tools.some((tool) => tool.status === 'running' || tool.status === 'draft')) {
    return '执行中'
  }
  if (tools.some((tool) => tool.status === 'error')) return '部分失败'
  return '已完成'
}

export function ToolGroup({ group }: { group: ToolGroupData }) {
  if (group.tools.length === 1) return <ToolCard tool={group.tools[0]} />

  const running = group.tools.some(
    (tool) => tool.status === 'running' || tool.status === 'draft',
  )
  return (
    <details className="tool-group" data-running={running}>
      <summary>
        <span className="tool-dot" />
        <strong>
          {toolTitle(group.name)} ×{group.tools.length}
        </strong>
        <em className="tool-status">{groupStatus(group.tools)}</em>
        <span className="tool-chevron">
          <IconChevron />
        </span>
      </summary>
      <div className="tool-group-body">
        {group.tools.map((tool, index) => (
          <ToolCard key={tool.key || index} tool={tool} dense />
        ))}
      </div>
    </details>
  )
}
