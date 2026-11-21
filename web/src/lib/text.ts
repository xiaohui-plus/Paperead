/** 目录页点线（dot leader）清理，以及无字形字符兜底。 */

/**
 * 数据里可能残留的无字形字符：
 * - U+E000–U+F8FF 私用区：PDF 把数学花体（𝒰/ℐ/𝒢/ℒ 等）塞进 PUA 且 ToUnicode 缺失时留下的；
 * - U+FFFD 替换符：编码转换失败；
 * - U+25A1 白方块：上游用方框当占位符。
 * 浏览器没有对应字形时会画成「□」，看起来就是乱码。
 */
const UNRENDERABLE_RE = /[\uE000-\uF8FF\uFFFD\u25A1]/g

/**
 * 把无字形字符替换成**等长空格**。
 * 长度不变，所以按字符偏移做的标注高亮、行内公式区间仍然有效；
 * 用在正文渲染这类依赖 offset 的场景。
 */
export function blankUnrenderable(text: string): string {
  if (!text) return text
  return text.replace(UNRENDERABLE_RE, ' ')
}

/** 直接删掉无字形字符；用在标题、图注、复制文本等不依赖 offset 的展示场景。 */
export function stripUnrenderable(text: string): string {
  if (!text) return text
  return text.replace(UNRENDERABLE_RE, '')
}

const DOT_LEADER_RE = /(?:[ \t]*[.．﹒·⋅•]){4,}|…{2,}/g
const DOT_LEADER_TEST_RE = /(?:[ \t]*[.．﹒·⋅•]){4,}|…{2,}/
const SPACE_RUN_RE = /[ \t]{2,}/g
const DOT_ONLY_CORE_RE = /[ \t\d.．﹒·⋅•\-–—_()[\]{}]/g
const DOT_CHAR_RE = /[.．﹒·⋅•]/g
const DOT_TAIL_RE = /^[\s\d\-–—()[\]{}]{0,8}$/
const DOT_PAGE_RE = /^\s*\d{1,3}(?!\d)/

/** 是否含目录式点线引导符（4 个以上连续点，允许点之间夹空格）。 */
export function hasDotLeader(text: string): boolean {
  return Boolean(text) && DOT_LEADER_TEST_RE.test(text)
}

/** 去掉目录条目的点线引导符，保留标题文字与页码。 */
export function stripDotLeaders(text: string): string {
  if (!text) return text
  const total = text.length
  return text
    .replace(DOT_LEADER_RE, (span: string, offset: number) => {
      // 目录引导符的判据：后面紧跟页码、后面只剩页码，或在文本里占比很大。
      // 长句子中间零星出现的 ". . . ." 更像数学省略号（\ldots），保留原样。
      const tail = text.slice(offset + span.length)
      if (
        DOT_TAIL_RE.test(tail) ||
        DOT_PAGE_RE.test(tail) ||
        span.length / total >= 0.25
      ) {
        return ' '
      }
      return span
    })
    .replace(SPACE_RUN_RE, ' ')
    .trim()
}

/** 整段是否只有点线（可能夹页码/括号），没有任何可读正文。 */
export function isDotLeaderOnly(text: string): boolean {
  const t = (text ?? '').trim()
  if (!t) return true
  const dots = (t.match(DOT_CHAR_RE) ?? []).length
  if (dots < 4) return false
  return t.replace(DOT_ONLY_CORE_RE, '').length <= 2
}