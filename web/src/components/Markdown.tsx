import { isValidElement, useEffect, useRef, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import { normalizeMath } from '../lib/math'
import { upgradeSvgMath } from '../lib/mermaidMath'

/**
 * 流式输出时补上未闭合的 Markdown 标记。
 *
 * 模型逐字返回时，`**粗体` 的后半截可能还没到；如果不补，页面上会直接显示
 * `**`，停止生成后也会永久留下半个标记。这里只处理代码块和粗体这两种最常见
 * 的情况，补的是「渲染用副本」，不改动回答本身。
 */
export function balanceMarkdown(src: string): string {
  const fenceCount = (src.match(/^```/gm) ?? []).length
  if (fenceCount % 2 === 1) return `${src}\n\`\`\``

  const prose = src
    .replace(/```[\s\S]*?```/g, '')
    .replace(/`[^`\n]*`/g, '')
    .replace(/\\\*/g, '')
  if (((prose.match(/\*\*/g) ?? []).length) % 2 === 1) return `${src}**`
  return src
}

type HastNode = {
  type?: string
  value?: string
  properties?: Record<string, unknown>
  children?: HastNode[]
}

/**
 * KaTeX 在 `throwOnError: false` 下会把语法错误输出成 `.katex-error` 红字源码。
 * 流式回答或模型改写的公式可能残缺，这里把错误节点换成纯文本，去掉红字样式。
 */
function dropKaTeXErrors(node: HastNode): void {
  if (!node.children?.length) return
  node.children = node.children.map((child) => {
    const klass = child.properties?.className
    const names = Array.isArray(klass)
      ? klass
      : typeof klass === 'string'
        ? [klass]
        : []
    if (names.includes('katex-error')) {
      const value = (child.children ?? []).map((item) => item.value ?? '').join('')
      return { type: 'text', value }
    }
    dropKaTeXErrors(child)
    return child
  })
}

function rehypeDropKaTeXErrors() {
  return (tree: HastNode) => {
    dropKaTeXErrors(tree)
  }
}

/** mermaid 体积较大，只在真正遇到图表时才加载。 */
function MermaidDiagram({ chart }: { chart: string }) {
  const host = useRef<HTMLDivElement>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    setFailed(false)
    void (async () => {
      try {
        const mermaid = (await import('mermaid')).default
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: 'default',
          fontFamily: 'inherit',
        })
        const id = `mermaid-${Math.random().toString(36).slice(2)}`
        const { svg } = await mermaid.render(id, chart)
        if (!cancelled && host.current) {
          host.current.innerHTML = svg
          upgradeSvgMath(host.current.querySelector('svg'))
        }
      } catch {
        if (!cancelled) setFailed(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [chart])

  if (failed) return <pre className="md-mermaid-error">{chart}</pre>
  return <div className="md-mermaid" ref={host} />
}

/** 从 <pre> 的子元素里认出 ```mermaid 代码块。 */
function mermaidChartOf(children: ReactNode): string | null {
  const list = Array.isArray(children) ? children : [children]
  for (const child of list) {
    if (!isValidElement(child)) continue
    const props = child.props as { className?: string; children?: ReactNode }
    if (typeof props.className === 'string' && props.className.includes('language-mermaid')) {
      return String(props.children ?? '')
    }
  }
  return null
}

/**
 * 助手回答的 Markdown 渲染。
 *
 * - react-markdown 默认不解析原始 HTML，模型输出里即使混入标签也不会执行；
 * - 表格在 344px 的窄面板里横向滚动；
 * - 公式交给 KaTeX（样式已在 main.tsx 引入）；模型常见的 `\(...\)`、
 *   `\[...\]`、`\begin{equation}` 等写法先统一成 `$` / `$$` 再渲染；
 * - remark-breaks 保留单换行，和旧版 pre-wrap 的观感一致。
 *
 * `breaks` 设为 false 时按标准 Markdown 解析，用于外部笔记：remark-breaks
 * 会把表格行之间的换行变成 <br>，导致 GFM 表格退化成纯文本。
 */
export function Markdown({ text, breaks = true }: { text: string; breaks?: boolean }) {
  const remarkPlugins = breaks
    ? [remarkGfm, remarkMath, remarkBreaks]
    : [remarkGfm, remarkMath]
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={[[rehypeKatex, { throwOnError: false, strict: false }], rehypeDropKaTeXErrors]}
        components={{
          a: ({ node, ...props }) => {
            void node
            return <a {...props} target="_blank" rel="noreferrer" />
          },
          table: ({ node, ...props }) => {
            void node
            return (
              <div className="md-table">
                <table {...props} />
              </div>
            )
          },
          pre: ({ children }) => {
            const chart = mermaidChartOf(children)
            if (chart) return <MermaidDiagram chart={chart} />
            return <pre>{children}</pre>
          },
        }}
      >
        {normalizeMath(text)}
      </ReactMarkdown>
    </div>
  )
}