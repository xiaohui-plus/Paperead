/**
 * 配置同步：桌面端把 `paperead.*` 设置推到后端数据目录，手机端连上后拉取模型配置。
 *
 * 主窗口跑在 `http://127.0.0.1:<port>`，localStorage 按 origin 隔离：端口一变
 * （旧版随机端口、或固定端口被占用顺延）所有设置都会像「升级后丢了」。数据目录
 * 与端口无关，所以这里把设置整份备份过去，启动时只补本地缺失的键。
 */
import { apiUrl, hasMobileServer, isMobileShell } from './mobile';

const STORAGE_KEY = 'paperead.models';
const PULLED_KEY = 'paperead.models.pulled';
const SETTINGS_PREFIX = 'paperead.';
const APP_SETTINGS_API = '/api/settings/app';
const APP_SETTINGS_LIMIT = 256 * 1024;

let pushTimer: number | null = null;

/** 只用于本机运行状态，换端口后重建即可，不必持久化。 */
const TRANSIENT_KEYS = new Set([
  PULLED_KEY,
  'paperead.update.check',
  'paperead.scholaread.last',
  'paperead.sync.exported',
]);

function isTransient(key: string): boolean {
  return TRANSIENT_KEYS.has(key) || key.startsWith('paperead.mobile.');
}

/** 模型配置单独走 model-settings.json，避免两份副本互相覆盖。 */
function isMirrored(key: string): boolean {
  return key.startsWith(SETTINGS_PREFIX) && key !== STORAGE_KEY && !isTransient(key);
}

/** 失败后重试，避免启动早期服务未就绪时把配置悄悄丢掉。 */
async function putSettings(path: string, body: string, attempts = 4): Promise<boolean> {
  for (let index = 0; index < attempts; index += 1) {
    try {
      const response = await fetch(apiUrl(path), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      if (response.ok) return true;
    } catch {
      // 服务还没起来，按退避重试
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1200 * (index + 1)));
  }
  return false;
}

function collectAppSettings(): Record<string, string> {
  const settings: Record<string, string> = {};
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (!key || !isMirrored(key)) continue;
      const value = localStorage.getItem(key);
      if (value !== null && value.length <= APP_SETTINGS_LIMIT) settings[key] = value;
    }
  } catch {
    // 隐私模式读不到，跳过本次备份
  }
  return settings;
}

function readRemote(payload: unknown): Record<string, unknown> | null {
  if (!payload || typeof payload !== 'object') return null;
  const settings = (payload as { settings?: unknown }).settings;
  return settings && typeof settings === 'object'
    ? (settings as Record<string, unknown>)
    : null;
}

/** 桌面端保存设置后调用（同源 PUT），失败不影响本地使用。 */
export function pushModelSettings(settings: unknown): void {
  if (isMobileShell()) return;
  if (typeof window === 'undefined') return;
  if (pushTimer !== null) window.clearTimeout(pushTimer);
  pushTimer = window.setTimeout(() => {
    pushTimer = null;
    void putSettings('/api/settings/models', JSON.stringify({ settings }));
  }, 800);
}

/**
 * 桌面端启动时先恢复一次模型配置。
 *
 * 主窗口是 `http://127.0.0.1:<port>`，旧版本端口随机，localStorage 会跟着换
 * 命名空间；数据目录里的 model-settings.json 才是稳定副本。本地为空时从后端
 * 拉回来，升级 / 换端口后不用重新填 Key。
 */
export async function restoreModelSettings(): Promise<boolean> {
  if (isMobileShell()) return false;
  if (typeof window === 'undefined') return false;
  try {
    const text = localStorage.getItem(STORAGE_KEY);
    if (text) {
      try {
        const parsed = JSON.parse(text) as { models?: unknown };
        if (Array.isArray(parsed?.models) && parsed.models.length > 0) {
          // 本地有配置：顺手补一份到数据目录，防止副本缺失后换端口读不到
          void putSettings('/api/settings/models', JSON.stringify({ settings: parsed }));
          return false;
        }
      } catch {
        // 坏数据继续走恢复流程
      }
    }
    const response = await fetch(apiUrl('/api/settings/models'));
    if (!response.ok) return false;
    const remote = readRemote(await response.json());
    if (!remote) return false;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(remote));
    return true;
  } catch {
    return false;
  }
}

/**
 * 桌面端启动时恢复整份设置：只补本地没有的键，不覆盖本次已经改过的值。
 *
 * 端口被占用顺延、或以后换端口时，主题、阅读偏好、研究模型选择等也能回来。
 */
export async function restoreAppSettings(): Promise<void> {
  if (isMobileShell() || typeof window === 'undefined') return;
  try {
    const response = await fetch(apiUrl(APP_SETTINGS_API), { cache: 'no-store' });
    if (!response.ok) return;
    const payload = (await response.json()) as { settings?: unknown };
    const settings = payload.settings;
    if (!settings || typeof settings !== 'object') return;
    for (const [key, value] of Object.entries(settings as Record<string, unknown>)) {
      if (typeof value !== 'string' || !isMirrored(key)) continue;
      if (value.length > APP_SETTINGS_LIMIT) continue;
      if (localStorage.getItem(key) === null) localStorage.setItem(key, value);
    }
  } catch {
    // 服务未就绪时下次启动再试
  }
}

let lastSnapshot = '';

function backupAppSettings(keepalive = false): void {
  const body = JSON.stringify({ settings: collectAppSettings() });
  if (body === lastSnapshot) return;
  if (keepalive) {
    lastSnapshot = body;
    void fetch(apiUrl(APP_SETTINGS_API), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: true,
    }).catch(() => undefined);
    return;
  }
  void putSettings(APP_SETTINGS_API, body, 2).then((ok) => {
    if (ok) lastSnapshot = body;
  });
}

/** 桌面端定时 + 退出前把 `paperead.*` 设置备份到数据目录。 */
export function startAppSettingsBackup(): void {
  if (isMobileShell() || typeof window === 'undefined') return;
  window.setInterval(() => backupAppSettings(), 5000);
  window.addEventListener('pagehide', () => backupAppSettings(true));
}

/**
 * 手机端启动时调用：把服务器上的配置写回本地。
 * 返回 true 表示本地有变化，调用方需要刷新一次让组件读到新配置。
 */
export async function pullModelSettings(): Promise<boolean> {
  if (!isMobileShell() || !hasMobileServer()) return false;
  if (typeof sessionStorage !== 'undefined' && sessionStorage.getItem(PULLED_KEY) === '1') {
    return false;
  }
  try {
    const response = await fetch(apiUrl('/api/settings/models'));
    if (!response.ok) return false;
    const remote = readRemote(await response.json());
    if (!remote) return false;
    const next = JSON.stringify(remote);
    if (localStorage.getItem(STORAGE_KEY) === next) return false;
    localStorage.setItem(STORAGE_KEY, next);
    sessionStorage.setItem(PULLED_KEY, '1');
    return true;
  } catch {
    return false;
  }
}
