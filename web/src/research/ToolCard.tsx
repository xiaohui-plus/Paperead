import { useState } from 'react'
import {
  IconBot,
  IconBook,
  IconChevron,
  IconCode,
  IconDownload,
  IconEdit,
  IconExternalLink,
  IconFileSearch,
  IconList,
  IconSearch,
  IconWrench,
} from '../components/icons'
import {
  asText,
  formatDuration,
  toolSubtitle,
  toolTitle,
  type ToolCall,
} from '../lib/researchTypes'

interface DiffEntry {
  path: string
  oldText: string | null
  newText: string
}

interface ReadLine {
  number: number
  text: string
}

interface ReadMeta {
  path: string
  offset?: number
  lines: ReadLine[]
}

/** 工具图标：按名称归类，未知工具回落到通用图标。 */
export function toolIcon(name: string) {
  if (name === 'read' || name === 'glob' || name === 'grep' || name === 'file_read') {
    return <IconFileSearch />
  }
  if (name === 'write' || name === 'edit' || name.includes('write')) return <IconEdit />
  if (name === 'pwsh' || name === 'bash' || name.startsWith('job_')) return <IconCode />
  if (name === 'web_fetch') return <IconExternalLink />
  if (name.startsWith('subagent') || name === 'send_message' || name === 'list_agents') {
    return <IconBot />
  }
  if (name.includes('search')) return <IconSearch />
  if (name === 'paper_read' || name === 'paper_get' || name === 'notes_read') return <IconBook />
  if (name.includes('import') || name.includes('ingest')) return <IconDownload />
  if (name.includes('read')) return <IconList />
  return <IconWrench />
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

function metaRecord(tool: ToolCall): Record<string, unknown> | null {
  return tool.meta && typeof tool.meta === 'object'
    ? (tool.meta as Record<string, unknown>)
    : null
}

const TERMINAL_TOOLS = new Set(['pwsh', 'bash', 'run_command'])

function isTerminalTool(name: string): boolean {
  return TERMINAL_TOOLS.has(name)
}

function terminalCommand(tool: ToolCall): string {
  if (tool.input && typeof tool.input === 'object') {
    const record = tool.input as Record<string, unknown>
    for (const key of ['command', 'cmd']) {
      if (typeof record[key] === 'string' && record[key].trim()) {
        return record[key].trim()
      }
    }
  }
  return (tool.argumentsText || '').trim()
}

/** DSH 的终端结果把退出码写在文本里；旧的自研终端则放在 meta。 */
function terminalExitCode(tool: ToolCall): number | null {
  const meta = metaRecord(tool)
  if (typeof meta?.exitCode === 'number') return meta.exitCode
  const text = asText(tool.result)
  const match =
    /\[exit code:\s*(-?\d+)\]/i.exec(text) ?? /exit\s+code[:\s]+(-?\d+)/i.exec(text)
  return match ? Number(match[1]) : null
}

function terminalOutput(tool: ToolCall): string {
  return asText(tool.result)
    .replace(/\[exit code:\s*-?\d+\]/gi, '')
    .replace(/exit\s+code[:\s]+-?\d+/gi, '')
    .trim()
}

function exitPill(tool: ToolCall) {
  if (!isTerminalTool(tool.name)) return null
  const code = terminalExitCode(tool)
  if (code === null) return null
  return (
    <span className="tool-pill" data-ok={code === 0 || undefined} data-fail={code !== 0 || undefined}>
      exit {code}
    </span>
  )
}

function TerminalBlock({ tool }: { tool: ToolCall }) {
  const command = terminalCommand(tool)
  const output = terminalOutput(tool)
  return (
    <div className="tool-terminal">
      {command && (
        <div className="tool-terminal-cmd">
          <span className="tool-terminal-prompt" aria-hidden="true">
            $
          </span>
          <pre>{command}</pre>
        </div>
      )}
      <pre className="tool-terminal-output">{output || '（无输出）'}</pre>
    </div>
  )
}

type WebSource = {
  url: string
  title?: string
  snippet?: string
  publishedAt?: string
}

/** web_search 的 presentationMeta 带结构化来源；web_fetch 带 url 与状态码。 */
function webSources(tool: ToolCall): WebSource[] {
  const meta = metaRecord(tool)
  const raw = meta?.sources
  if (!Array.isArray(raw)) return []
  const sources: WebSource[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    if (typeof record.url !== 'string') continue
    sources.push({
      url: record.url,
      title: typeof record.title === 'string' ? record.title : undefined,
      snippet: typeof record.snippet === 'string' ? record.snippet : undefined,
      publishedAt: typeof record.publishedAt === 'string' ? record.publishedAt : undefined,
    })
  }
  return sources
}

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

function WebBlock({ tool }: { tool: ToolCall }) {
  const meta = metaRecord(tool)
  if (tool.name === 'web_fetch') {
    const url = typeof meta?.url === 'string' ? meta.url : ''
    const status = typeof meta?.statusCode === 'number' ? meta.statusCode : null
    return (
      <div className="tool-web">
        <div className="tool-web-head">
          {url && <span className="tool-web-target">{hostOf(url)}</span>}
          {status !== null && (
            <span className="tool-pill" data-ok={status < 400 || undefined} data-fail={status >= 400 || undefined}>
              {status}
            </span>
          )}
        </div>
        <pre className="tool-web-body">{terminalOutput(tool) || '（无正文）'}</pre>
      </div>
    )
  }
  const sources = webSources(tool)
  if (!sources.length) return null
  return (
    <div className="tool-web">
      <ol className="tool-web-sources">
        {sources.map((source, index) => (
          <li key={`${source.url}-${index}`}>
            <a href={source.url} target="_blank" rel="noreferrer">
              <span className="tool-web-title">{source.title || source.url}</span>
              <span className="tool-web-host">{hostOf(source.url)}</span>
              <IconExternalLink />
            </a>
            {source.snippet && <p className="tool-web-snippet">{source.snippet}</p>}
          </li>
        ))}
      </ol>
    </div>
  )
}

function isWebTool(name: string): boolean {
  return name === 'web_search' || name === 'web_fetch'
}

/** write/edit 的工具结果 meta 带 diffs；渲染成行级增删视图。 */
function diffEntries(tool: ToolCall): DiffEntry[] | null {
  const meta = metaRecord(tool)
  const raw = meta?.diffs
  if (!Array.isArray(raw)) return null
  const entries: DiffEntry[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    if (typeof record.path !== 'string') continue
    entries.push({
      path: record.path,
      oldText: typeof record.oldText === 'string' ? record.oldText : null,
      newText: typeof record.newText === 'string' ? record.newText : '',
    })
  }
  return entries.length ? entries : null
}

/** read 的工具结果 meta 带行号文本；用于阅读卡片。 */
function readMeta(tool: ToolCall): ReadMeta | null {
  const meta = metaRecord(tool)
  const raw = meta?.lines
  if (typeof meta?.path !== 'string' || !Array.isArray(raw)) return null
  const lines: ReadLine[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    if (typeof record.number !== 'number' || typeof record.text !== 'string') continue
    lines.push({ number: record.number, text: record.text })
  }
  return lines.length
    ? {
        path: meta.path,
        offset: typeof meta.offset === 'number' ? meta.offset : undefined,
        lines,
      }
    : null
}

type DiffRow = { kind: 'same' | 'add' | 'del'; text: string }

/** 行级朴素 LCS；超大文件退化成「旧文全部删除 + 新文全部新增」。 */
function lineDiff(oldText: string | null, newText: string): DiffRow[] {
  const before = oldText ? oldText.split('\n') : []
  const after = newText.split('\n')
  if (before.length * after.length > 250_000) {
    return [
      ...before.map((text) => ({ kind: 'del' as const, text })),
      ...after.map((text) => ({ kind: 'add' as const, text })),
    ]
  }
  const dp: number[][] = Array.from({ length: before.length + 1 }, () =>
    new Array<number>(after.length + 1).fill(0),
  )
  for (let i = before.length - 1; i >= 0; i -= 1) {
    for (let j = after.length - 1; j >= 0; j -= 1) {
      dp[i][j] =
        before[i] === after[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const rows: DiffRow[] = []
  let i = 0
  let j = 0
  while (i < before.length && j < after.length) {
    if (before[i] === after[j]) {
      rows.push({ kind: 'same', text: before[i] })
      i += 1
      j += 1
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ kind: 'del', text: before[i] })
      i += 1
    } else {
      rows.push({ kind: 'add', text: after[j] })
      j += 1
    }
  }
  while (i < before.length) {
    rows.push({ kind: 'del', text: before[i] })
    i += 1
  }
  while (j < after.length) {
    rows.push({ kind: 'add', text: after[j] })
    j += 1
  }
  return rows
}

const DIFF_PREVIEW_ROWS = 240
const READ_PREVIEW_ROWS = 400

function DiffBlock({ entry }: { entry: DiffEntry }) {
  const rows = lineDiff(entry.oldText, entry.newText)
  const hidden = Math.max(0, rows.length - DIFF_PREVIEW_ROWS)
  return (
    <div className="tool-diff">
      <div className="tool-diff-head">
        <span>{entry.path}</span>
        {entry.oldText === null && <em>新建</em>}
      </div>
      <pre className="tool-diff-body">
        {rows.slice(0, DIFF_PREVIEW_ROWS).map((row, index) => (
          <span key={index} className="tool-diff-line" data-kind={row.kind}>
            {row.kind === 'add' ? '+ ' : row.kind === 'del' ? '- ' : '  '}
            {row.text || ' '}
          </span>
        ))}
        {hidden > 0 && (
          <span className="tool-diff-line" data-kind="more">
            … 还有 {hidden} 行未显示
          </span>
        )}
      </pre>
    </div>
  )
}

function ReadBlock({ meta }: { meta: ReadMeta }) {
  const hidden = Math.max(0, meta.lines.length - READ_PREVIEW_ROWS)
  return (
    <div className="tool-read">
      <div className="tool-read-head">
        <span>{meta.path}</span>
        {meta.offset ? <em>从第 {meta.offset} 行继续</em> : null}
      </div>
      <pre className="tool-read-body">
        {meta.lines.slice(0, READ_PREVIEW_ROWS).map((line) => (
          <span key={line.number} className="tool-read-line">
            <span className="tool-read-number">{line.number}</span>
            {line.text || ' '}
          </span>
        ))}
        {hidden > 0 && (
          <span className="tool-read-line" data-more="true">
            … 还有 {hidden} 行未显示
          </span>
        )}
      </pre>
    </div>
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
  const [open, setOpen] = useState(false)
  const failed = tool.status === 'error'
  const error = asText(tool.error)
  const summary = failed ? error || '工具调用失败' : toolSubtitle(tool)
  const diffs = failed ? null : diffEntries(tool)
  const read = failed ? null : readMeta(tool)
  const terminal = isTerminalTool(tool.name)
  const web = !failed && isWebTool(tool.name)
  const specialized = Boolean(diffs || read || terminal || web)
  return (
    <div className="tool-row" data-status={tool.status} data-dense={dense} data-open={open}>
      <button
        type="button"
        className="tool-row-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
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
      </button>
      {open && (
        <div className="tool-body">
          {diffs?.map((entry, index) => (
            <DiffBlock key={`${entry.path}-${index}`} entry={entry} />
          ))}
          {read && <ReadBlock meta={read} />}
          {terminal && <TerminalBlock tool={tool} />}
          {web && <WebBlock tool={tool} />}
          {!specialized && (
            <div className="tool-io">
              <span className="tool-io-label">IN</span>
              <pre className="tool-io-text">{detailText(tool)}</pre>
            </div>
          )}
          {failed && error && (
            <div className="tool-io" data-error="true">
              <span className="tool-io-label">OUT</span>
              <pre className="tool-io-text">{error}</pre>
            </div>
          )}
          {!failed && tool.result && !specialized && (
            <div className="tool-io" data-result="true">
              <span className="tool-io-label">OUT</span>
              <pre className="tool-io-text">{resultText(tool)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}