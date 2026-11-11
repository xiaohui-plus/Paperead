/**
 * 阅读界面偏好：自动阅读重点、原文/译文字号、默认阅读模式。
 * 只保存在本机；字号通过 CSS 变量即时作用到阅读页正文。
 */
import type { ViewMode } from '../types'

const AUTO_KEY = 'paperead.autoReadingHighlights'
const FONTS_KEY = 'paperead.readingFonts'
const MODE_KEY = 'paperead.readingMode'

export interface ReadingFonts {
  /** 原文字号倍率 */
  en: number
  /** 译文字号倍率 */
  zh: number
}

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