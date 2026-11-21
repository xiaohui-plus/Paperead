import type { InlineMath } from '../types'

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
/* ── Markdown 渲染前的公式定界符归一化 ─────────────────────
 *
 * remark-math 只认 `$...$` / `$$...$$`，但模型和论文文本里常见的写法是
 * `\(...\)`、`\[...\]`、`\begin{equation}`、`\begin{align}`，这些会原样显示成
 * LaTeX 源码。这里把各种写法统一成 remark-math 能识别的形式，代码块里的
 * 内容保持原样。
 */

const CODE_OR_INLINE = /(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)/g
const EXISTING_DISPLAY = /\$\$[\s\S]*?\$\$/g

// 顶层数学环境：单独成块、需要整块转成 $$...$$
const MATH_ENV =
  String.raw`equation\*?|displaymath|math|align\*?|alignat\*?|gather\*?|multline\*?|eqnarray\*?|flalign\*?|split|alignedat\*?|aligned|gathered|cases|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|matrix|array`

const ENV_RE = new RegExp(
  String.raw`\\begin\{(${MATH_ENV})\}([\s\S]*?)\\end\{\1\}`,
  'g',
)
const BRACKET_RE = new RegExp(String.raw`(?<!\\)\\\[([\s\S]*?)(?<!\\)\\\]`, 'g')
const PAREN_RE = new RegExp(String.raw`(?<!\\)\\\(([\s\S]*?)(?<!\\)\\\)`, 'g')

// 独立成行的裸 LaTeX（整行都没有写 $ 定界符）
const BARE_MATH_LINE =
  /^\s*\\(?:frac|dfrac|tfrac|sqrt|sum|prod|int|oint|lim|left|right|mathbb|mathcal|mathbf|mathrm|mathit|text|alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa|lambda|mu|nu|xi|rho|sigma|tau|upsilon|phi|chi|psi|omega|partial|nabla|cdot|times|pm|mp|leq|geq|neq|approx|equiv|sim|infty|log|exp|sin|cos|tan|max|min|arg|det|binom)\b[^$]*$/

// 裸公式行里只允许数学符号，避免把「\frac 是除法命令」这类说明文字吞进公式
const BARE_MATH_CHARS = /^[\s\\a-zA-Z0-9{}^_=+\-*/()[\].,:;|&<>!]+$/

function stripUnsupported(body: string): string {
  return body
    .replace(/\\(?:label|nonumber|notag)\{[^{}]*\}/g, '')
    .replace(/\\(?:nonumber|notag)\b/g, '')
}

function asDisplay(body: string): string {
  const text = body.trim()
  if (!text) return ''
  if (text.includes('\n')) return `\n\n$$\n${text}\n$$\n\n`
  return `$$${text}$$`
}

function envToDisplay(name: string, body: string): string {
  const base = name.replace(/\*$/, '')
  const inner = stripUnsupported(body).trim()
  if (!inner) return ''

  if (base === 'equation' || base === 'displaymath' || base === 'math') {
    return asDisplay(inner)
  }
  if (base === 'multline' || base === 'eqnarray' || base === 'flalign') {
    return asDisplay(`\\begin{gathered}\n${inner}\n\\end{gathered}`)
  }
  return asDisplay(`\\begin{${name}}\n${inner}\n\\end{${name}}`)
}

export function normalizeMath(input: string): string {
  if (!input || (!input.includes('\\') && !input.includes('$'))) return input

  const saved: string[] = []
  const keep = (value: string) => {
    saved.push(value)
    return `\u0000${saved.length - 1}\u0000`
  }

  // 代码块 / 行内代码 / 已经是 $$...$$ 的内容不参与改写
  let text = input.replace(CODE_OR_INLINE, keep)
  text = text.replace(EXISTING_DISPLAY, keep)
  // 先处理 \[...\]：里面的 aligned 等环境随外层一起包进同一个 $$...$$
  text = text.replace(BRACKET_RE, (_full, body: string) => keep(asDisplay(body)))
  text = text.replace(ENV_RE, (_full, name: string, body: string) =>
    keep(envToDisplay(name, body)),
  )
  text = text.replace(PAREN_RE, (_full, body: string) => {
    const value = body.trim()
    if (!value) return ''
    return value.includes('\n') ? keep(asDisplay(value)) : keep(`$${value}$`)
  })
  text = text
    .split('\n')
    .map((line) =>
      BARE_MATH_LINE.test(line) && BARE_MATH_CHARS.test(line) ? keep(asDisplay(line)) : line,
    )
    .join('\n')

  return text.replace(/\u0000(\d+)\u0000/g, (_full, index: string) => saved[Number(index)] ?? '')
}
/* ── 译文行内公式映射 ───────────────────────────────────── */

/** 去掉空白，同时保留「压缩文本下标 → 原文本下标」的映射。 */
function compactForMath(text: string): { norm: string; map: number[] } {
  const chars: string[] = []
  const map: number[] = []
  for (let i = 0; i < text.length; i += 1) {
    if (!/\s/.test(text[i])) {
      chars.push(text[i])
      map.push(i)
    }
  }
  map.push(text.length)
  return { norm: chars.join(''), map }
}

/**
 * 模型的译文里通常会把公式原样保留（`xt ∈ { xfront t , xdown t }`），
 * 但行内公式的 start/end 是相对英文原文的。这里按公式原文片段在译文里
 * 顺序查找（忽略空白差异），找到后用同一份 latex / 裁图渲染中文里的公式。
 */
/** 私用区字符：PDF 把花体数学字母塞进 PUA，行内公式的 source 一定带着它。 */
const ANCHOR_RE = /[\uE000-\uF8FF]/

/** 公式符号后面可能紧跟的字母/数字（含希腊字母与 Unicode 数学字母）。 */
const TRAILING_ATOM = /^[0-9A-Za-z\u0370-\u03ff\u1d400-\u1d7ff]$/u

/**
 * 宽松兜底：模型常把公式之间的 "and" 译成「和」，整段 source 就对不上了。
 * 此时用公式里的私用区字符当锚点定位，并把紧随其后的字母/数字一起吞进公式区间
 * （`\ue238𝑠𝑡𝑟和…` 只吞到「和」为止），公式不会丢，也不会留下 `𝑠𝑡𝑟` 碎片。
 */
function looseRange(norm: string, from: number, raw: string): { start: number; end: number } | null {
  const anchor = raw.match(ANCHOR_RE)?.[0]
  if (!anchor) return null
  const at = norm.indexOf(anchor, from)
  if (at < 0) return null
  let end = at + 1
  // 数学字母（𝑠/𝑐 等）是代理对，必须按码点遍历，否则单个 code unit 匹配不上。
  for (const ch of norm.slice(end)) {
    if (!TRAILING_ATOM.test(ch)) break
    end += ch.length
  }
  return { start: at, end }
}

export function mapInlineMathToTranslation(
  source: string,
  translated: string,
  inlineMath: InlineMath[] = [],
): InlineMath[] {
  if (!translated || !inlineMath.length) return []
  const target = compactForMath(translated)
  const result: InlineMath[] = []
  let from = 0

  for (const item of [...inlineMath].sort((a, b) => a.start - b.start)) {
    const raw = (item.source || source.slice(item.start, item.end) || '').trim()
    const needle = compactForMath(raw).norm
    if (!needle) continue
    const exact = target.norm.indexOf(needle, from)
    const relaxed = exact < 0 ? looseRange(target.norm, from, raw) : null
    const range = exact >= 0 ? { start: exact, end: exact + needle.length } : relaxed
    if (!range) continue
    const start = target.map[range.start]
    const end = target.map[range.end]
    if (end <= start) continue
    result.push({ ...item, start, end, source: translated.slice(start, end) })
    from = range.end
  }
  return result
}
