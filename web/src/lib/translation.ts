/**
 * 译文目标语言：只影响阅读页的翻译，不影响问答/导读（仍按中文回答）。
 * 保存在本机 localStorage，切换后由阅读页按新语言重新请求翻译。
 */

export interface TranslationLang {
  key: string
  label: string
}

export const TRANSLATION_LANGS: TranslationLang[] = [
  { key: 'zh-Hans', label: '简体中文' },
  { key: 'zh-Hant', label: '繁體中文' },
  { key: 'ja', label: '日本語' },
  { key: 'ko', label: '한국어' },
  { key: 'fr', label: 'Français' },
  { key: 'de', label: 'Deutsch' },
  { key: 'es', label: 'Español' },
  { key: 'ru', label: 'Русский' },
]

const KEY = 'paperead.translationTarget'
export const DEFAULT_TARGET = 'zh-Hans'

export function loadTargetLang(): string {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved && TRANSLATION_LANGS.some((lang) => lang.key === saved)) return saved
  } catch {
    // 隐私模式读不到 localStorage：用默认语言
  }
  return DEFAULT_TARGET
}

export function saveTargetLang(key: string): void {
  try {
    localStorage.setItem(KEY, key)
  } catch {
    // 写不了就只在本次会话生效
  }
  window.dispatchEvent(
    new CustomEvent('paperead:translation-changed', { detail: key }),
  )
}

export function langLabel(key: string): string {
  return TRANSLATION_LANGS.find((lang) => lang.key === key)?.label ?? TRANSLATION_LANGS[0].label
}
