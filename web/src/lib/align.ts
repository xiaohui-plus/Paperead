import type { AnnotationPair } from './library'

interface SentenceRange {
  start: number
  end: number
}

const SENTENCE_RE: Record<'en' | 'zh', RegExp> = {
  en: /[^.!?;\n]+[.!?;]+["')\]]*/g,
  zh: /[^。！？；!?;\n]+[。！？；!?;]+[”’」』）】]*/g,
}

/** 按语言切句，返回每句在文本里的区间；尾部没有结束标点的一段也算一句。 */
export function sentenceRanges(text: string, lang: 'en' | 'zh'): SentenceRange[] {
  if (!text.trim()) return []
  const ranges: SentenceRange[] = []
  let cursor = 0
  for (const match of text.matchAll(SENTENCE_RE[lang])) {
    if (match.index == null) continue
    ranges.push({ start: match.index, end: match.index + match[0].length })
    cursor = match.index + match[0].length
  }
  if (cursor < text.length && text.slice(cursor).trim()) {
    ranges.push({ start: cursor, end: text.length })
  }
  return ranges
}

function trimRange(text: string, start: number, end: number): SentenceRange {
  let from = start
  let to = end
  while (from < to && /\s/.test(text[from])) from += 1
  while (to > from && /\s/.test(text[to - 1])) to -= 1
  return { start: from, end: to }
}

/**
 * 把一侧（原文/译文）的选区映射到另一侧。
 *
 * 论文译文是按段落整体翻译的，段落内没有可靠的逐句对齐，
 * 这里按「句子序号比例」对齐：划中哪几句原文，就取译文里对应的那几句。
 * 句数一致时等价于逐句对应；句数不同时按比例收敛，宁可多覆盖一句，
 * 也避免译文只被涂了一半。
 */
export function mapRangeToOther(
  text: string,
  other: string,
  start: number,
  end: number,
  source: 'en' | 'zh',
): AnnotationPair | null {
  if (!text || !other) return null
  const from = Math.max(0, Math.min(start, text.length))
  const to = Math.max(from, Math.min(end, text.length))
  if (to <= from) return null

  const otherLang = source === 'en' ? 'zh' : 'en'
  const target = sentenceRanges(other, otherLang)
  if (!target.length) return null

  const src = sentenceRanges(text, source)
  let first = 0
  let last = target.length - 1
  if (src.length) {
    const indexAt = (pos: number) => {
      for (let i = 0; i < src.length; i += 1) {
        if (pos < src[i].end) return i
      }
      return src.length - 1
    }
    const i = indexAt(from)
    const j = indexAt(Math.max(from, to - 1))
    const scale = target.length / src.length
    first = Math.min(target.length - 1, Math.floor(i * scale))
    last = Math.min(target.length - 1, Math.max(first, Math.ceil((j + 1) * scale) - 1))
  } else {
    const scale = target.length / text.length
    first = Math.min(target.length - 1, Math.floor(from * scale))
    last = Math.min(target.length - 1, Math.max(first, Math.ceil(to * scale) - 1))
  }

  const range = trimRange(other, target[first].start, target[last].end)
  if (range.end <= range.start) return null
  return {
    source: otherLang,
    start: range.start,
    end: range.end,
    quote: other.slice(range.start, range.end),
  }
}

/** 标注的另一侧片段是否还能在当前文本里对上（重译后可能需要重算）。 */
export function pairFits(text: string, pair: AnnotationPair | null | undefined): boolean {
  if (!pair) return false
  if (!text) return false
  if (text.slice(pair.start, pair.end) === pair.quote) return true
  return Boolean(pair.quote) && text.indexOf(pair.quote) >= 0
}