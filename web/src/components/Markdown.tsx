import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'

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

/**
 * 助手回答的 Markdown 渲染。
 *
 * - react-markdown 默认不解析原始 HTML，模型输出里即使混入标签也不会执行；
 * - 表格在 344px 的窄面板里横向滚动；
 * - 公式交给 KaTeX（样式已在 main.tsx 引入）；
 * - remark-breaks 保留单换行，和旧版 pre-wrap 的观感一致。
 */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, remarkBreaks]}
        rehypePlugins={[[rehypeKatex, { throwOnError: false, strict: false }]]}
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
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
