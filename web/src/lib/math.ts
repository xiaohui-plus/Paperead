// 把含 LaTeX 的纯文本切成「文本段 / 公式段」，供页面用 KaTeX 渲染。
// 输入是清洗后的纯字符串，因此只需处理分隔符。

export type MathSegment =
  | { type: 'text'; value: string }
  | { type: 'math'; value: string; display: boolean }

// 分隔符优先级：$$..$$ > \[..\] > $..$ > \(..\)
// 行内公式不允许跨行，避免把整段文本误吞；display 公式允许跨行。
const MATH_RE = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\$([^$\n]+?)\$|\\\(([\s\S]+?)\\\)/g

// 裸 LaTeX（源数据里没写 $ 定界符，例如 arXiv 老论文标题 d_{x^2-y^2}）的保守启发式：
// 只有当一段记号里出现带花括号的下/上标 _{...} / ^{...} 时才当公式；
// 普通文本里的 x^2、y_1、foo_bar 都不会被误判。
const BRACE = String.raw`\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}`
const ATOM = String.raw`(?:[A-Za-z0-9}\]\)]|\\[A-Za-z]+)`
const SUB = String.raw`(?:(?:_|\^)(?:${BRACE}|${ATOM}))`
const BRACE_SUB = String.raw`(?:(?:_|\^)${BRACE})`
const BARE_TERM = String.raw`${ATOM}+${SUB}*${BRACE_SUB}${SUB}*`
const BARE_RUN = String.raw`${BARE_TERM}(?:[+\-]${BARE_TERM})*`
const BARE_MATH_RE = new RegExp(BARE_RUN, 'g')

function splitDelimited(input: string): MathSegment[] {
  const segments: MathSegment[] = []
  let last = 0
  let m: RegExpExecArray | null

  MATH_RE.lastIndex = 0
  while ((m = MATH_RE.exec(input)) !== null) {
    const start = m.index
    // \$ 是字面量美元符，不作为公式起点
    if (start > 0 && input[start - 1] === '\\') {
      MATH_RE.lastIndex = start + 1
      continue
    }
    if (start > last) segments.push({ type: 'text', value: input.slice(last, start) })

    const [full, dd, brDisplay, inline, brInline] = m
    const display = dd !== undefined || brDisplay !== undefined
    segments.push({ type: 'math', value: dd ?? brDisplay ?? inline ?? brInline ?? '', display })
    last = start + (full?.length ?? 0)
  }

  if (last < input.length) segments.push({ type: 'text', value: input.slice(last) })
  return segments
}

function splitBare(input: string): MathSegment[] {
  const out: MathSegment[] = []
  let last = 0
  let m: RegExpExecArray | null

  BARE_MATH_RE.lastIndex = 0
  while ((m = BARE_MATH_RE.exec(input)) !== null) {
    const value = m[0] ?? ''
    if (!value) break
    if (m.index > last) out.push({ type: 'text', value: input.slice(last, m.index) })
    out.push({ type: 'math', value, display: false })
    last = m.index + value.length
  }

  if (last < input.length) out.push({ type: 'text', value: input.slice(last) })
  return out.length > 0 ? out : [{ type: 'text', value: input }]
}

export function splitMath(input: string): MathSegment[] {
  if (!input) return []
  return splitDelimited(input).flatMap((seg) =>
    seg.type === 'text' ? splitBare(seg.value) : [seg],
  )
}
