/** 目录页点线（dot leader）清理。 */

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