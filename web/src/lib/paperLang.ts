/**
 * 论文原文语种识别：中文文献默认不自动翻译，外文文献默认自动翻译。
 * 只采样标题和正文开头，不读全文，打开论文时不增加等待。
 */
import type { Paper } from '../types'

export type PaperLang = 'zh' | 'other' | 'unknown'

/** 正文采样上限：块数与字符数取先到者 */
const SAMPLE_BLOCKS = 60
const SAMPLE_CHARS = 12000
/** 判为中文文献所需的最少汉字数与汉字占比（中英混排的论文也能覆盖） */
const MIN_CJK = 40
const MIN_CJK_RATIO = 0.2

const CJK_RE = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/g
const LATIN_RE = /[A-Za-z]/g
const KANA_RE = /[\u3040-\u30ff]/g
const HANGUL_RE = /[\uac00-\ud7af]/g

function countChars(text: string, re: RegExp): number {
  return (text.match(re) || []).length
}

/**
 * 判断论文原文语种。
 * - 正文可采样到足够文字时按正文判定；
 * - 正文不足（图表页、解析不全）时退回标题；
 * - 日文/韩文含汉字，不能仅凭汉字就当成中文文献。
 */
export function detectPaperLanguage(
  paper: Pick<Paper, 'title' | 'blocks'>,
): PaperLang {
  let sample = ''
  let blocks = 0
  for (const block of paper.blocks ?? []) {
    if (blocks >= SAMPLE_BLOCKS || sample.length >= SAMPLE_CHARS) break
    const text = (block.text ?? '').trim()
    if (!text) continue
    blocks += 1
    sample += `${text}\n`
  }

  const cjk = countChars(sample, CJK_RE)
  const latin = countChars(sample, LATIN_RE)
  const kana = countChars(sample, KANA_RE)
  const hangul = countChars(sample, HANGUL_RE)

  if (cjk + latin >= 80) {
    if (kana >= 20 && kana >= cjk * 0.3) return 'other'
    if (hangul >= 20) return 'other'
    if (cjk >= MIN_CJK && cjk / (cjk + latin) >= MIN_CJK_RATIO) return 'zh'
    return 'other'
  }

  const title = paper.title ?? ''
  const titleCjk = countChars(title, CJK_RE)
  const titleLatin = countChars(title, LATIN_RE)
  // 日文假名、韩文谚文都不是中文；标题里有一个就能排除中文文献
  if (countChars(title, KANA_RE) >= 2) return 'other'
  if (countChars(title, HANGUL_RE) >= 2) return 'other'
  if (titleCjk >= 4 && titleCjk >= titleLatin) return 'zh'
  if (titleCjk + titleLatin >= 4) return 'other'
  return 'unknown'
}

/** 论文语言决定的自动翻译默认值：中文关、外文开；总开关关闭时一律不开。 */
export function defaultAutoTranslateForPaper(
  lang: PaperLang,
  masterEnabled: boolean,
): boolean {
  if (!masterEnabled) return false
  return lang !== 'zh'
}