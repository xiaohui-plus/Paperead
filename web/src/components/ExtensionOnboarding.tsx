import { useCallback, useEffect, useState } from 'react'
import { listPlugins, startPairingWait } from '../lib/plugins'
import { isDesktopApp } from '../lib/update'
import { loadExtensionSetup } from '../lib/browserExtension'
import { useDialogDrag } from '../lib/useDialogDrag'
import { ExtensionInstaller } from './ExtensionInstaller'

const PROMPT_KEY = 'paperead.extension-wizard.shown'

function alreadyShown(): boolean {
  try {
    return localStorage.getItem(PROMPT_KEY) === '1'
  } catch {
    return true
  }
}

function markShown() {
  try {
    localStorage.setItem(PROMPT_KEY, '1')
  } catch {
    /* 本地存储不可用时最多多弹一次，不影响使用 */
  }
}

/**
 * 首次启动的浏览器插件安装向导。
 *
 * 安装包已经内置了扩展，用户不需要解压或去仓库下载；向导按「选浏览器 →
 * 加载扩展 → 完成连接」三页展示，避免把状态、步骤、排查说明堆在一张长卡片里。
 */
export function ExtensionOnboarding() {
  const [open, setOpen] = useState(false)
  const [connected, setConnected] = useState(0)
  const drag = useDialogDrag<HTMLDivElement>()

  useEffect(() => {
    if (!isDesktopApp() || alreadyShown()) return
    let cancelled = false

    const tryOpen = async () => {
      const setup = await loadExtensionSetup()
      if (cancelled) return
      const browsers = setup?.browsers ?? []
      if (browsers.length === 0) {
        // 没检测到浏览器就没必要打扰；下次启动再判断。
        return
      }
      try {
        const plugins = await listPlugins()
        if (cancelled) return
        if (plugins.length > 0) {
          markShown()
          return
        }
      } catch {
        /* 后端还没起来：稍后重试一次 */
      }
      waitForTourThen(() => {
        if (!cancelled) setOpen(true)
      })
    }

    const timer = window.setTimeout(() => void tryOpen(), 4200)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      const plugins = await listPlugins()
      setConnected(plugins.length)
      if (plugins.length > 0) markShown()
    } catch {
      /* 保持当前状态 */
    }
  }, [])

  useEffect(() => {
    if (!open) return
    void refresh()
    const timer = window.setInterval(() => void refresh(), 2500)
    return () => window.clearInterval(timer)
  }, [open, refresh])

  const dismiss = () => {
    markShown()
    setOpen(false)
  }

  if (!open) return null

  return (
    <div className="ext-wizard" role="dialog" aria-modal aria-labelledby="ext-wizard-title">
      <div className="ext-wizard-card" ref={drag.ref} {...drag.props}>
        <div className="ext-wizard-head" data-drag-handle>
          <div>
            <h2 id="ext-wizard-title">装一个浏览器插件</h2>
            <p>
              三步装好：选浏览器 → 加载扩展 → 自动连接。扩展已经随安装包放在电脑里，
              不用下载或解压，随时可以在「设置 → 插件与连接」里重新打开。
            </p>
          </div>
          <button className="icon-btn" onClick={dismiss} title="关闭" aria-label="关闭">
            ×
          </button>
        </div>

        <ExtensionInstaller
          variant="onboarding"
          connected={connected}
          onDone={dismiss}
          initialPage={previewPage()}
          onStartWait={() => {
            void startPairingWait().catch(() => undefined)
          }}
          onRefreshConnection={() => void refresh()}
        />
      </div>
    </div>
  )
}

/** 开发预览：?page=1/2 直接打开指定分页，生产构建会被静态替换为 0。 */
function previewPage(): number {
  if (!import.meta.env.DEV) return 0
  const value = Number(new URLSearchParams(window.location.search).get('page') || 0)
  return Number.isFinite(value) ? Math.max(0, Math.min(2, Math.trunc(value))) : 0
}

/** 新手教程选择卡还在屏幕上时先等它关掉，避免两个弹层叠在一起。 */
function waitForTourThen(run: () => void, attempt = 0) {
  const gate = document.querySelector('.tour-gate')
  if (!gate) {
    run()
    return
  }
  if (attempt >= 6) {
    run()
    return
  }
  window.setTimeout(() => waitForTourThen(run, attempt + 1), 2500)
}