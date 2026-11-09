/**
 * 文献检索的本地配置。
 *
 * CORE 没有免密钥入口，必须由用户自己申请；Semantic Scholar 不填也能用，
 * 但配置 Key 后限流额度更高。密钥只存在浏览器 localStorage，启动和保存设置时
 * 推给本地服务（内存），服务端不落盘；环境变量 PAPEREAD_CORE_API_KEY /
 * PAPEREAD_S2_API_KEY 仍然可用作默认值。
 */

const STORAGE_KEY = 'paperead.scholar'

export interface ScholarSettings {
  coreApiKey: string
  s2ApiKey: string
}

export function loadScholarSettings(): ScholarSettings {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') as {
      coreApiKey?: unknown
      s2ApiKey?: unknown
    }
    return {
      coreApiKey: typeof raw.coreApiKey === 'string' ? raw.coreApiKey : '',
      s2ApiKey: typeof raw.s2ApiKey === 'string' ? raw.s2ApiKey : '',
    }
  } catch {
    return { coreApiKey: '', s2ApiKey: '' }
  }
}

/** 保存并立即同步给本地服务；同步完成后通知检索页刷新数据源可用性。 */
export function saveScholarSettings(next: ScholarSettings): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        coreApiKey: next.coreApiKey.trim(),
        s2ApiKey: next.s2ApiKey.trim(),
      }),
    )
  } catch {
    // 隐私模式下存不住，本次会话内仍会同步给本地服务
  }
  void syncScholarConfig().finally(() => {
    window.dispatchEvent(new Event('paperead:scholar-config'))
  })
}

/** 把配置推给本地服务；服务未启动时静默，等下次启动或保存再同步。 */
export async function syncScholarConfig(): Promise<void> {
  try {
    const { coreApiKey, s2ApiKey } = loadScholarSettings()
    await fetch('/api/config/scholar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ coreApiKey, s2ApiKey }),
    })
  } catch {
    // 本地服务未启动：下次启动或保存设置时会再同步
  }
}
