import {
  formatDuration,
  toolSubtitle,
  toolTitle,
  type ToolCall,
} from '../lib/researchTypes'

function statusText(tool: ToolCall): string {
  if (tool.status === 'draft' || tool.status === 'running') return '执行中'
  if (tool.status === 'error') return '失败'
  return '已完成'
}

function detailText(tool: ToolCall): string {
  if (tool.input !== undefined) {
    try {
      return JSON.stringify(tool.input, null, 2)
    } catch {
      return String(tool.input)
    }
  }
  return tool.argumentsText || '（参数尚未到达）'
}

export function ToolCard({ tool, dense = false }: { tool: ToolCall; dense?: boolean }) {
  const subtitle = toolSubtitle(tool)
  const duration =
    tool.startedAt && tool.endedAt
      ? formatDuration(tool.endedAt - tool.startedAt)
      : ''
  return (
    <details className="tool-card" data-status={tool.status} data-dense={dense}>
      <summary>
        <span className="tool-dot" />
        <strong>{toolTitle(tool.name)}</strong>
        {subtitle && <span className="tool-subtitle">{subtitle}</span>}
        <em className="tool-status">
          {statusText(tool)}
          {duration ? ` · ${duration}` : ''}
        </em>
      </summary>
      <div className="tool-detail">
        {tool.error && <p className="tool-error">{tool.error}</p>}
        <pre>{detailText(tool)}</pre>
      </div>
    </details>
  )
}
