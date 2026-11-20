/**
 * 阅读界面偏好：阅读字体、自动阅读重点、原文/译文字号、默认阅读模式。
 * 只保存在本机；字体与字号通过 CSS 变量即时作用到阅读页正文。
 */
import type { ViewMode } from '../types'

const AUTO_KEY = 'paperead.autoReadingHighlights'
const AUTO_TRANSLATE_KEY = 'paperead.autoTranslate'
/** 按论文记住用户手动切换过的自动翻译；未记录时按论文语种给默认值 */
const PAPER_AUTO_TRANSLATE_KEY = 'paperead.autoTranslate.byPaper'
const FONTS_KEY = 'paperead.readingFonts'
/** 原文/译文字体分开存储；旧版只有一个 readerFont 键，读取时做迁移 */
const READER_FONT_PAIR_KEY = 'paperead.readerFontPair'
const LEGACY_READER_FONT_KEY = 'paperead.readerFont'
const MODE_KEY = 'paperead.readingMode'

export interface ReadingFonts {
  /** 原文字号倍率 */
  en: number
  /** 译文字号倍率 */
  zh: number
}

export interface ReaderFontOption {
  key: string
  label: string
  /** 完整的 font-family 栈：先列本机常见字体，最后回退到通用字族 */
  stack: string
}

/** 原文与译文可以分别选择字体；两侧互不影响。 */
export interface ReaderFontPair {
  source: string
  target: string
}

/** 中文回退到宋体：macOS 用 Songti SC，Windows 用 SimSun / 宋体。 */
const SONG_TI = '"Songti SC", "SimSun", "宋体", "Noto Serif CJK SC", "Source Han Serif SC"'

/** 常见中英文字体；西文与数字走首选拉丁字体，中文回退宋体。 */
export const READER_FONTS: ReaderFontOption[] = [
  {
    key: 'georgia',
    label: 'Georgia + 宋体（英文报刊，默认）',
    stack: `Georgia, "Times New Roman", ${SONG_TI}, serif`,
  },
  {
    key: 'times',
    label: 'Times New Roman + 宋体（学术论文）',
    stack: `"Times New Roman", Times, ${SONG_TI}, serif`,
  },
  {
    key: 'serif',
    label: '宋体 / 思源宋体（纯中文衬线）',
    stack: '"Source Han Serif SC", "Noto Serif CJK SC", "Songti SC", "SimSun", "宋体", Georgia, serif',
  },
  {
    key: 'sans',
    label: '微软雅黑 / 思源黑体（无衬线）',
    stack: '"Microsoft YaHei", "微软雅黑", "PingFang SC", "Source Han Sans SC", "Noto Sans CJK SC", "Segoe UI", Arial, sans-serif',
  },
  {
    key: 'kai',
    label: '楷体',
    stack: '"KaiTi", "楷体", "Kaiti SC", STKaiti, "AR PL UKai CN", serif',
  },
  {
    key: 'fangsong',
    label: '仿宋',
    stack: '"FangSong", "仿宋", "FangSong_GB2312", STFangsong, serif',
  },
  {
    key: 'heiti',
    label: '黑体',
    stack: '"SimHei", "黑体", "Heiti SC", STHeiti, "Source Han Sans SC", sans-serif',
  },
  {
    key: 'dengxian',
    label: '等线',
    stack: '"DengXian", "等线", "Microsoft YaHei", sans-serif',
  },
  {
    key: 'arial',
    label: 'Arial / Helvetica',
    stack: 'Arial, Helvetica, "Microsoft YaHei", "微软雅黑", sans-serif',
  },
]

/** 默认使用报刊感的 Georgia，中文自动落到宋体。 */
export const DEFAULT_READER_FONT = 'georgia'

export const FONT_MIN = 0.85
export const FONT_MAX = 1.35

export const DEFAULT_FONTS: ReadingFonts = { en: 1, zh: 1 }

function clampFont(value: unknown, fallback: number): number {
  const num = Number(value)
  if (!Number.isFinite(num)) return fallback
  return Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round(num * 100) / 100))
}

export function loadAutoHighlights(): boolean {
  try {
    return localStorage.getItem(AUTO_KEY) !== '0'
  } catch {
    // 隐私模式读不到 localStorage：保持默认开启
    return true
  }
}

export function saveAutoHighlights(enabled: boolean): void {
  try {
    localStorage.setItem(AUTO_KEY, enabled ? '1' : '0')
  } catch {
    // 写不了就只在本次会话生效
  }
  window.dispatchEvent(
    new CustomEvent('paperead:auto-highlights-changed', { detail: enabled }),
  )
}

export function loadAutoTranslate(): boolean {
  try {
    return localStorage.getItem(AUTO_TRANSLATE_KEY) !== '0'
  } catch {
    // 隐私模式读不到 localStorage：保持默认开启
    return true
  }
}

export function saveAutoTranslate(enabled: boolean): void {
  try {
    localStorage.setItem(AUTO_TRANSLATE_KEY, enabled ? '1' : '0')
    // 总开关变了，各论文的手动记忆作废，重新按语种给默认值
    localStorage.removeItem(PAPER_AUTO_TRANSLATE_KEY)
  } catch {
    // 写不了就只在本次会话生效
  }
  window.dispatchEvent(
    new CustomEvent('paperead:auto-translate-changed', { detail: enabled }),
  )
}

/** 读取这篇论文上次手动切换过的自动翻译；没手动切过返回 null。 */
export function loadPaperAutoTranslate(paperId: string): boolean | null {
  try {
    const raw = localStorage.getItem(PAPER_AUTO_TRANSLATE_KEY)
    if (!raw) return null
    const map = JSON.parse(raw) as Record<string, unknown>
    return typeof map[paperId] === 'boolean' ? map[paperId] : null
  } catch {
    return null
  }
}

/** 顶栏手动切换：只记这篇论文，不改全局总开关，也不影响其他论文。 */
export function savePaperAutoTranslate(paperId: string, enabled: boolean): void {
  try {
    const raw = localStorage.getItem(PAPER_AUTO_TRANSLATE_KEY)
    const map = raw ? (JSON.parse(raw) as Record<string, unknown>) : {}
    map[paperId] = enabled
    localStorage.setItem(PAPER_AUTO_TRANSLATE_KEY, JSON.stringify(map))
  } catch {
    // 写不了就只在本次会话生效
  }
}

export function loadReadingFonts(): ReadingFonts {
  try {
    const raw = localStorage.getItem(FONTS_KEY)
    if (!raw) return { ...DEFAULT_FONTS }
    const parsed = JSON.parse(raw) as Partial<ReadingFonts>
    return {
      en: clampFont(parsed.en, DEFAULT_FONTS.en),
      zh: clampFont(parsed.zh, DEFAULT_FONTS.zh),
    }
  } catch {
    return { ...DEFAULT_FONTS }
  }
}

/** 把字号倍率写到根元素，阅读页正文按变量缩放；切换设置后立即生效。 */
export function applyReadingFonts(fonts: ReadingFonts = loadReadingFonts()): void {
  const root = document.documentElement
  root.style.setProperty('--en-font-scale', String(fonts.en))
  root.style.setProperty('--zh-font-scale', String(fonts.zh))
}

export function saveReadingFonts(fonts: ReadingFonts): void {
  const next = {
    en: clampFont(fonts.en, DEFAULT_FONTS.en),
    zh: clampFont(fonts.zh, DEFAULT_FONTS.zh),
  }
  try {
    localStorage.setItem(FONTS_KEY, JSON.stringify(next))
  } catch {
    // 写不了就只在本次会话生效
  }
  applyReadingFonts(next)
  window.dispatchEvent(
    new CustomEvent('paperead:reading-fonts-changed', { detail: next }),
  )
}

function normalizeReaderFontKey(key: unknown): string | null {
  return typeof key === 'string' && READER_FONTS.some((font) => font.key === key)
    ? key
    : null
}

export function loadReaderFonts(): ReaderFontPair {
  const fallback: ReaderFontPair = {
    source: DEFAULT_READER_FONT,
    target: DEFAULT_READER_FONT,
  }
  try {
    const raw = localStorage.getItem(READER_FONT_PAIR_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<ReaderFontPair>
      return {
        source: normalizeReaderFontKey(parsed.source) ?? DEFAULT_READER_FONT,
        target: normalizeReaderFontKey(parsed.target) ?? DEFAULT_READER_FONT,
      }
    }
    // 旧版只有一个字体键：原文和译文都沿用用户之前的选择
    const legacy = normalizeReaderFontKey(
      localStorage.getItem(LEGACY_READER_FONT_KEY),
    )
    if (legacy) return { source: legacy, target: legacy }
  } catch {
    // 隐私模式读不到 localStorage：用默认字体
  }
  return fallback
}

export function readerFontStack(key: string): string {
  return (
    READER_FONTS.find((font) => font.key === key)?.stack ??
    READER_FONTS.find((font) => font.key === DEFAULT_READER_FONT)?.stack ??
    READER_FONTS[0].stack
  )
}

export function readerFontLabel(key: string): string {
  return (
    READER_FONTS.find((font) => font.key === key)?.label ??
    READER_FONTS.find((font) => font.key === DEFAULT_READER_FONT)?.label ??
    READER_FONTS[0].label
  )
}

/** 把原文/译文字体栈分别写到根元素；只换字形，不动字号与布局。 */
export function applyReaderFonts(fonts: ReaderFontPair = loadReaderFonts()): void {
  const root = document.documentElement
  root.style.setProperty('--reader-font-source', readerFontStack(fonts.source))
  root.style.setProperty('--reader-font-target', readerFontStack(fonts.target))
}

export function saveReaderFonts(fonts: ReaderFontPair): void {
  const next: ReaderFontPair = {
    source: normalizeReaderFontKey(fonts.source) ?? DEFAULT_READER_FONT,
    target: normalizeReaderFontKey(fonts.target) ?? DEFAULT_READER_FONT,
  }
  try {
    localStorage.setItem(READER_FONT_PAIR_KEY, JSON.stringify(next))
    localStorage.removeItem(LEGACY_READER_FONT_KEY)
  } catch {
    // 写不了就只在本次会话生效
  }
  applyReaderFonts(next)
  window.dispatchEvent(
    new CustomEvent('paperead:reader-font-changed', { detail: next }),
  )
}

export function loadDefaultReadingMode(): ViewMode {
  try {
    const value = localStorage.getItem(MODE_KEY)
    return value === 'en' || value === 'zh' ? value : 'bilingual'
  } catch {
    return 'bilingual'
  }
}

export function saveDefaultReadingMode(mode: ViewMode): void {
  try {
    localStorage.setItem(MODE_KEY, mode)
  } catch {
    // 写不了就只在本次会话生效
  }
  window.dispatchEvent(
    new CustomEvent('paperead:reading-mode-changed', { detail: mode }),
  )
}