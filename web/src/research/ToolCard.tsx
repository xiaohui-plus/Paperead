import {
  IconBook,
  IconChevron,
  IconDownload,
  IconEdit,
  IconList,
  IconSearch,
  IconSpark,
} from '../components/icons'
import {
  asText,
  formatDuration,
  toolSubtitle,
  toolTitle,
  type ToolCall,
} from '../lib/researchTypes'

/** 工具图标：按名称归类，未知工具回落到通用图标。 */
export function toolIcon(name: string) {
  if (name.includes('search')) return <IconSearch />
  if (name === 'paper_read' || name === 'paper_get') return <IconBook />
  if (name.includes('import') || name.includes('ingest')) return <IconDownload />
  if (name.includes('write')) return <IconEdit />
  if (name.includes('read')) return <IconList />
  return <IconSpark />
}

function doneText(tool: ToolCall): string {
  if (!tool.startedAt || !tool.endedAt) return '已完成'
  const elapsed = tool.endedAt - tool.startedAt
  return elapsed >= 1000 ? formatDuration(elapsed) : '已完成'
}

function statusText(tool: ToolCall): string {
  if (tool.status === 'draft' || tool.status === 'running') return '运行中'
  if (tool.status === 'error') return '失败'
  return doneText(tool)
}

function exitPill(tool: ToolCall) {
  const meta =
    tool.meta && typeof tool.meta === 'object'
      ? (tool.meta as Record<string, unknown>)
      : null
  if (meta?.kind !== 'terminal') return null
  const code = meta.exitCode
  if (typeof code !== 'number') return null
  return (
    <span className="tool-pill" data-ok={code === 0 || undefined} data-fail={code !== 0 || undefined}>
      exit {code}
    </span>
  )
}

/** 工具结果若是 JSON，展开时缩进显示；截断导致解析失败则原样返回。 */
function resultText(tool: ToolCall): string {
  const raw = asText(tool.result)
  const trimmed = raw.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return raw
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2)
  } catch {
    return raw
  }
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
  const failed = tool.status === 'error'
  const error = asText(tool.error)
  const summary = failed ? error || '工具调用失败' : toolSubtitle(tool)
  return (
    <details className="tool-row" data-status={tool.status} data-dense={dense}>
      <summary>
        <span className="tool-leading">{toolIcon(tool.name)}</span>
        <strong className="tool-title">{toolTitle(tool.name)}</strong>
        {summary && (
          <>
            <span className="tool-sep" aria-hidden="true" />
            <span className="tool-summary" data-error={failed || undefined}>
              {summary}
            </span>
          </>
        )}
        {exitPill(tool)}
        <em className="tool-suffix">{statusText(tool)}</em>
        <span className="tool-chevron">
          <IconChevron />
        </span>
      </summary>
      <div className="tool-body">
        <div className="tool-io">
          <span className="tool-io-label">IN</span>
          <pre className="tool-io-text">{detailText(tool)}</pre>
        </div>
        {failed && error && (
          <div className="tool-io" data-error="true">
            <span className="tool-io-label">OUT</span>
            <pre className="tool-io-text">{error}</pre>
          </div>
        )}
        {!failed && tool.result && (
          <div className="tool-io" data-result="true">
            <span className="tool-io-label">OUT</span>
            <pre className="tool-io-text">{resultText(tool)}</pre>
          </div>
        )}
      </div>
    </details>
  )
}
