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
 * 安装包已经内置了扩展，用户不需要解压或去仓库下载；这里负责在检测到
 * 本机浏览器且尚未连接插件时弹一次，把「选浏览器 → 打开扩展页」做到一键。
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
              装好后，在网页上看到论文或 PDF 可以直接存进 Paperead，读的时候也能顺手加批注。
              扩展已经随安装包放在电脑里，不用下载或解压。
            </p>
          </div>
          <button className="icon-btn" onClick={dismiss} title="关闭" aria-label="关闭">
            ×
          </button>
        </div>

        <ExtensionInstaller
          variant="onboarding"
          connected={connected}
          onStartWait={() => {
            void startPairingWait().catch(() => undefined)
          }}
          onRefreshConnection={() => void refresh()}
        />

        <div className="ext-wizard-actions">
          <button className="btn primary" onClick={dismiss}>
            {connected > 0 ? '完成' : '稍后再说'}
          </button>
          <span>也可以随时在「设置 → 插件与连接」里重新安装。</span>
        </div>
      </div>
    </div>
  )
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