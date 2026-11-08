/**
 * 本地偏好迁移：0.1.0 及更早版本的 localStorage key 前缀是 `paper-reflow.`，
 * 改名 Paperead 后统一成 `paperead.`。只复制、不删除旧 key，回退版本也不丢配置。
 */
const LEGACY_PREFIX = 'paper-reflow.'
const PREFIX = 'paperead.'

export function migrateLegacyStorage(): void {
  try {
    const keys = Object.keys(localStorage).filter((key) =>
      key.startsWith(LEGACY_PREFIX),
    )
    for (const key of keys) {
      const target = `${PREFIX}${key.slice(LEGACY_PREFIX.length)}`
      if (localStorage.getItem(target) !== null) continue
      const value = localStorage.getItem(key)
      if (value !== null) localStorage.setItem(target, value)
    }
  } catch {
    // 隐私模式/配额满：迁移失败不影响正常使用
  }
}
