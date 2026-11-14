/**
 * 正文引用标记的识别与归一匹配。
 *
 * 这里只做「字符串 → 标记」和「标记 ↔ 标记」的纯计算，不依赖 React，
 * 方便单测。识别与解析是两条独立链路：这里宽松识别，后端负责落条目；
 * 归一化保证 `(Kipf & Welling 2016a)` 与 `Kipf and Welling 2016a` 能对上。
 */

/** 姓名：首字母大写，允许连字符与撇号（Liben-Nowell / O'Neil）。 */
const AUTHOR = "[A-Z][A-Za-z\\-'\u2019]+"
/** 作者连接：`A, B` / `A, and B` / `A and B` / `A & B`。 */
const JOIN = "(?:\\s*,\\s*(?:and\\s+|&\\s+)?|\\s+and\\s+|\\s*&\\s*)"

/** 数字引用 [12] / [3-5]，或任意作者数的作者-年份 (Louis, Jacob, and Salehi-Abari 2023)。 */
export const CITE_RE = new RegExp(
  `\\[\\d{1,3}(?:\\s*[,\\-–]\\s*\\d{1,3})*\\]` +
    `|\\(${AUTHOR}(?:${JOIN}${AUTHOR})*(?:\\s+et\\s+al\\.?)?[\\s,]*((?:19|20)\\d{2}[a-z]?)\\)`,
  'g',
)

/** 段落里是否存在可点击的引用标记。 */
export function hasCitation(text: string): boolean {
  CITE_RE.lastIndex = 0
  return CITE_RE.test(text)
}

/** 去掉括号/空白/大小写差异，把两种写法的同一个引用变成同一个 key。 */
export function normalizeMarker(value: string): string {
  return value
    .replace(/[（）()[\]]/g, '')
    .replace(/\bet\s*al\b\.?/gi, 'etal')
    .replace(/&/g, 'and')
    .replace(/[\s,;:.，。；：]/g, '')
    .toLowerCase()
}

/** 数字标记展开：[12,13] / [3-5] → 文献编号列表。 */
function numericMarker(value: string): number[] {
  if (!/^[\s[\]()\d,\-–]+$/.test(value)) return []
  const out: number[] = []
  for (const part of value.match(/\d{1,3}(?:\s*[\-–]\s*\d{1,3})?/g) ?? []) {
    const range = part.match(/^(\d+)\s*[\-–]\s*(\d+)$/)
    if (range) {
      const a = Number(range[1])
      const b = Number(range[2])
      if (b > a && b - a <= 20) {
        for (let n = a; n <= b; n += 1) out.push(n)
      } else {
        out.push(a, b)
      }
    } else {
      out.push(Number(part))
    }
  }
  return out
}

/** 两个 marker 是否指同一条引用（忽略括号/空白/and 与 & 的差异）。 */
export function markerMatches(a: string, b: string): boolean {
  const x = normalizeMarker(a)
  const y = normalizeMarker(b)
  if (!x || !y) return false
  if (x === y) return true
  if (x.length >= 6 && y.length >= 6 && (x.includes(y) || y.includes(x))) return true
  const na = numericMarker(a)
  const nb = numericMarker(b)
  return na.length > 0 && nb.length > 0 && na.some((n) => nb.includes(n))
}

/**
 * 在后端返回的整段引用里找出当前点击的那一条。
 * 先精确归一匹配，再退到数字编号/包含匹配。
 */
export function matchMarkers<T extends { marker: string; ref?: { index?: number } }>(
  items: T[] | null | undefined,
  marker: string,
): T[] {
  if (!items || items.length === 0) return []
  const target = normalizeMarker(marker)
  if (!target) return []

  const exact = items.filter((item) => normalizeMarker(item.marker) === target)
  if (exact.length) return exact

  const nums = numericMarker(marker)
  if (nums.length) {
    const numeric = items.filter((item) => {
      const idx = item.ref?.index
      if (typeof idx === 'number' && nums.includes(idx)) return true
      return numericMarker(item.marker).some((n) => nums.includes(n))
    })
    if (numeric.length) return numeric
  }

  return items.filter((item) => {
    const value = normalizeMarker(item.marker)
    return value.length >= 6 && (target.includes(value) || value.includes(target))
  })
}