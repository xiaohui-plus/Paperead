import type { AskToolCall, AskToolItem } from '../lib/library'
import { IconBook, IconChevron, IconList, IconSearch, IconSpark } from './icons'

const TOOL_TITLES: Record<string, string> = {
  paper_search: '检索论文',
  web_search: '联网搜索',
  web_fetch: '读取网页',
}

function toolIcon(name: string) {
  if (name === 'paper_search') return <IconBook />
  if (name === 'web_fetch') return <IconList />
  if (name.includes('search')) return <IconSearch />
  return <IconSpark />
}

function statusText(tool: AskToolCall): string {
  if (tool.status === 'running') return '运行中'
  if (tool.status === 'error') return '失败'
  const ms = tool.meta?.elapsedMs
  if (ms && ms >= 1000) return `${(ms / 1000).toFixed(1)} 秒`
  return '已完成'
}

function HitRow({
  item,
  onJump,
}: {
  item: AskToolItem
  onJump?: (blockId: string) => void
}) {
  const label = <span className="tool-hit-label">{item.label}</span>
  const title = (
    <span className="tool-hit-title">
      {item.title || (item.page ? `第 ${item.page} 页` : '论文段落')}
    </span>
  )
  const snippet = item.snippet ? (
    <span className="tool-hit-snippet">{item.snippet}</span>
  ) : null

  if (item.url) {
    return (
      <a className="tool-hit" href={item.url} target="_blank" rel="noreferrer">
        {label}
        {title}
        {snippet}
      </a>
    )
  }
  if (!item.blockId) {
    return (
      <div className="tool-hit" data-static="true">
        {label}
        {title}
        {snippet}
      </div>
    )
  }
  return (
    <button
      type="button"
      className="tool-hit"
      onClick={() => item.blockId && onJump?.(item.blockId)}
    >
      {label}
      {title}
      {snippet}
    </button>
  )
}

/** 阅读页问答的检索步骤：与研究页 ToolCard 同构，结果换成可点击的论文段落/网页。 */
export function ReaderToolCard({
  tool,
  onJump,
}: {
  tool: AskToolCall
  onJump?: (blockId: string) => void
}) {
  const items = tool.items ?? []
  const failed = tool.status === 'error'
  return (
    <details className="tool-row" data-status={tool.status} data-reader="true">
      <summary>
        <span className="tool-leading">{toolIcon(tool.name)}</span>
        <strong className="tool-title">
          {TOOL_TITLES[tool.name] ?? tool.name}
        </strong>
        {tool.summary && (
          <>
            <span className="tool-sep" aria-hidden="true" />
            <span className="tool-summary" data-error={failed || undefined}>
              {tool.summary}
            </span>
          </>
        )}
        <em className="tool-suffix">{statusText(tool)}</em>
        <span className="tool-chevron">
          <IconChevron />
        </span>
      </summary>
      <div className="tool-body">
        <div className="tool-io">
          <span className="tool-io-label">IN</span>
          <pre className="tool-io-text">
            {JSON.stringify(tool.input ?? {}, null, 2)}
          </pre>
        </div>
        {items.length > 0 && (
          <div className="tool-io" data-result="true">
            <span className="tool-io-label">OUT</span>
            <ul className="tool-hits">
              {items.map((item, index) => (
                <li key={`${item.label}-${index}`}>
                  <HitRow item={item} onJump={onJump} />
                </li>
              ))}
            </ul>
          </div>
        )}
        {failed && tool.error && (
          <div className="tool-io" data-error="true">
            <span className="tool-io-label">OUT</span>
            <pre className="tool-io-text">{tool.error}</pre>
          </div>
        )}
      </div>
    </details>
  )
}