/**
 * 论文原文语种识别：用来决定打开论文时是否自动翻译。
 *
 * 规则从「中文关、外文开」升级为「与目标译文语种一致就关，不一致就开」：
 * 英文论文 + 目标 English、日文论文 + 目标日本語，都不再触发无意义的翻译。
 * 只采样标题和正文开头，不读全文，打开论文时不增加等待。
 */
import type { Paper } from '../types'

export type PaperLang =
  | 'zh-Hans'
  | 'zh-Hant'
  | 'ja'
  | 'ko'
  | 'ru'
  | 'en'
  | 'fr'
  | 'de'
  | 'es'
  | 'unknown'

export const PAPER_LANG_LABELS: Record<PaperLang, string> = {
  'zh-Hans': '简体中文',
  'zh-Hant': '繁體中文',
  ja: '日语',
  ko: '韩语',
  ru: '俄语',
  en: '英语',
  fr: '法语',
  de: '德语',
  es: '西班牙语',
  unknown: '未知语种',
}

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
const CYRILLIC_RE = /[\u0400-\u04ff]/g
/** 只有繁体字形才出现的常用字，用来区分简繁中文 */
const TRADITIONAL_RE = /[這個們為與學發實驗數據結論網絡優訓練預測評估構設計應場轉換語標準檢索資料庫開環境處顯示]/g

const LATIN_PROFILES: {
  lang: PaperLang
  words: Set<string>
  chars: RegExp | null
}[] = [
  {
    lang: 'en',
    words: new Set([
      'the', 'of', 'and', 'to', 'in', 'is', 'that', 'for', 'with', 'as', 'on',
      'by', 'this', 'are', 'from', 'we', 'it', 'be', 'an', 'or', 'their', 'has',
    ]),
    chars: null,
  },
  {
    lang: 'fr',
    words: new Set([
      'le', 'la', 'les', 'des', 'une', 'dans', 'pour', 'avec', 'est', 'sont',
      'cette', 'ces', 'nous', 'vous', 'leur', 'aux', 'du', 'sur', 'par', 'plus',
    ]),
    chars: /[àâçéèêëîïôûùüÿœ]/g,
  },
  {
    lang: 'de',
    words: new Set([
      'der', 'die', 'das', 'und', 'ist', 'nicht', 'mit', 'für', 'von', 'den',
      'dem', 'des', 'ein', 'eine', 'auf', 'werden', 'wird', 'durch', 'wir', 'sich',
    ]),
    chars: /[äöüß]/g,
  },
  {
    lang: 'es',
    words: new Set([
      'el', 'los', 'las', 'una', 'que', 'en', 'por', 'para', 'con', 'del',
      'como', 'más', 'este', 'esta', 'son', 'entre', 'se', 'su', 'sus', 'al',
    ]),
    chars: /[ñáíóú¿¡]/g,
  },
]

function countChars(text: string, re: RegExp): number {
  return (text.match(re) || []).length
}

/** 拉丁字母语言按功能词频率 + 特有字符打分；证据不足时返回 unknown，宁可翻译也不误判。 */
function detectLatinLanguage(text: string): PaperLang {
  const lower = text.toLowerCase()
  const tokens = lower.match(/[a-zà-öø-ÿœ]+/g) ?? []
  if (tokens.length < 20) return 'unknown'
  const total = tokens.length
  const scored = LATIN_PROFILES.map((profile) => {
    let hits = 0
    for (const token of tokens) {
      if (profile.words.has(token)) hits += 1
    }
    let score = hits / total
    if (profile.chars) {
      score += (countChars(lower, profile.chars) / Math.max(1, lower.length)) * 12
    }
    return { lang: profile.lang, score }
  }).sort((a, b) => b.score - a.score)

  const best = scored[0]
  const second = scored[1]?.score ?? 0
  if (best.lang === 'en') {
    // 英文是学术论文的主力语种：功能词足够多就按英文处理
    return best.score >= 0.1 ? 'en' : 'unknown'
  }
  return best.score >= 0.1 && best.score >= second * 1.35 ? best.lang : 'unknown'
}

/** 从任意文本判断语种；标题等短文本证据不足时返回 unknown。 */
export function detectLanguageFromText(text: string): PaperLang {
  const cjk = countChars(text, CJK_RE)
  const latin = countChars(text, LATIN_RE)
  const kana = countChars(text, KANA_RE)
  const hangul = countChars(text, HANGUL_RE)
  const cyrillic = countChars(text, CYRILLIC_RE)

  // 日文/韩文都含汉字，必须先看假名与谚文
  if (kana >= 20 && kana >= cjk * 0.3) return 'ja'
  if (hangul >= 20) return 'ko'
  if (cyrillic >= 40 && cyrillic >= latin * 0.3) return 'ru'
  if (cjk >= MIN_CJK && cjk / (cjk + latin) >= MIN_CJK_RATIO) {
    return countChars(text, TRADITIONAL_RE) >= 4 ? 'zh-Hant' : 'zh-Hans'
  }
  if (latin < 80) return 'unknown'
  return detectLatinLanguage(text)
}

/** 正文不足时退到标题：只能可靠识别中日韩，拉丁语种留给 unknown。 */
function detectFromTitle(title: string): PaperLang {
  const cjk = countChars(title, CJK_RE)
  const latin = countChars(title, LATIN_RE)
  if (countChars(title, KANA_RE) >= 2) return 'ja'
  if (countChars(title, HANGUL_RE) >= 2) return 'ko'
  if (cjk >= 4 && cjk >= latin) {
    return countChars(title, TRADITIONAL_RE) >= 1 ? 'zh-Hant' : 'zh-Hans'
  }
  return 'unknown'
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

  const enough =
    countChars(sample, CJK_RE) +
      countChars(sample, LATIN_RE) +
      countChars(sample, KANA_RE) +
      countChars(sample, HANGUL_RE) +
      countChars(sample, CYRILLIC_RE) >=
    80
  if (enough) return detectLanguageFromText(sample)
  return detectFromTitle(paper.title ?? '')
}

const CHINESE = new Set<PaperLang>(['zh-Hans', 'zh-Hant'])

/** 原文与目标译文是否同一种语言；中文简繁之间视为一致，不来回转写。 */
export function sameLanguage(paperLang: PaperLang, targetLang: string): boolean {
  if (paperLang === 'unknown') return false
  if (targetLang === 'unknown') return false
  if (CHINESE.has(paperLang) && CHINESE.has(targetLang as PaperLang)) return true
  return paperLang === targetLang
}

/**
 * 论文语言决定的自动翻译默认值：与目标语种一致时关闭，不一致时打开；
 * 设置里的「打开论文自动翻译」仍是总开关，关掉后一律不自动翻。
 */
export function defaultAutoTranslateForPaper(
  lang: PaperLang,
  masterEnabled: boolean,
  targetLang: string,
): boolean {
  if (!masterEnabled) return false
  return !sameLanguage(lang, targetLang)
}