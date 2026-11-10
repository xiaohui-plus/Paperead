/**
 * 模型配置同步：桌面端把 `paperead.models` 推到后端，手机端连上后拉取覆盖本地。
 *
 * 手机离开电脑后要能自己调模型做翻译/问答，所以配置必须落到手机上；
 * 后端只是中转，顺便让重装的手机也能一键恢复。
 */
import { apiUrl, hasMobileServer, isMobileShell } from './mobile';

const STORAGE_KEY = 'paperead.models';
const PULLED_KEY = 'paperead.models.pulled';

let pushTimer: number | null = null;

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
    void fetch(apiUrl('/api/settings/models'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    }).catch(() => undefined);
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
        if (Array.isArray(parsed?.models) && parsed.models.length > 0) return false;
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
