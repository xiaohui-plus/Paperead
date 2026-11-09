/**
 * 自动阅读设置：是否在翻译完成后自动提取研究目标 / 方法 / 结论。
 * 只保存在本机；默认开启，关闭后不再发起新的抽取请求（已生成的重点保留）。
 */

const KEY = 'paperead.autoReadingHighlights'

export function loadAutoHighlights(): boolean {
  try {
    return localStorage.getItem(KEY) !== '0'
  } catch {
    // 隐私模式读不到 localStorage：保持默认开启
    return true
  }
}

export function saveAutoHighlights(enabled: boolean): void {
  try {
    localStorage.setItem(KEY, enabled ? '1' : '0')
  } catch {
    // 写不了就只在本次会话生效
  }
  window.dispatchEvent(
    new CustomEvent('paperead:auto-highlights-changed', { detail: enabled }),
  )
}
