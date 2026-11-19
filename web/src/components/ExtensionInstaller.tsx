import { useCallback, useEffect, useMemo, useState } from 'react'
import braveIcon from '../assets/browser-icons/brave.svg'
import chromeIcon from '../assets/browser-icons/googlechrome.svg'
import edgeIcon from '../assets/browser-icons/microsoftedge.svg'
import firefoxIcon from '../assets/browser-icons/firefox.svg'
import {
  copyText,
  loadExtensionSetup,
  openBrowserExtensions,
  openExtensionFolder,
  type BrowserInfo,
  type ExtensionSetup,
} from '../lib/browserExtension'

const ICONS: Record<string, string> = {
  chrome: chromeIcon,
  edge: edgeIcon,
  brave: braveIcon,
  firefox: firefoxIcon,
}

interface Props {
  /** 设置页里嵌在面板中；onboarding 用于首次启动向导，按钮更大。 */
  variant?: 'settings' | 'onboarding'
  connected: number
  /** 一键安装后开始等待扩展自动配对。 */
  onStartWait?: () => void
  /** 手动重新检测（父组件刷新插件列表）。 */
  onRefreshConnection?: () => void
}

export function ExtensionInstaller({
  variant = 'settings',
  connected,
  onStartWait,
  onRefreshConnection,
}: Props) {
  const [setup, setSetup] = useState<ExtensionSetup | null>(null)
  const [loading, setLoading] = useState(true)
  const [active, setActive] = useState<BrowserInfo | null>(null)
  const [copied, setCopied] = useState<'idle' | 'ok' | 'fail'>('idle')
  const [opened, setOpened] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    const next = await loadExtensionSetup()
    setSetup(next)
    setLoading(false)
    if (next && next.browsers.length) {
      setActive((prev) => {
        if (prev && next.browsers.some((browser) => browser.id === prev.id)) {
          return prev
        }
        return next.browsers.find((browser) => browser.family === 'chromium') ?? next.browsers[0]
      })
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const browsers = setup?.browsers ?? []

  const dirFor = useCallback(
    (family: string | undefined) => {
      if (!setup) return ''
      return family === 'firefox' ? setup.firefox_dir || '' : setup.chromium_dir || ''
    },
    [setup],
  )

  const activeDir = useMemo(() => dirFor(active?.family), [active, dirFor])

  const install = async (browser: BrowserInfo) => {
    setError('')
    setActive(browser)
    setOpened(false)
    setCopied('idle')

    const dir = dirFor(browser.family)
    // 先复制再启动浏览器：剪贴板要落在同一次点击的用户手势里。
    if (dir) {
      const ok = await copyText(dir)
      setCopied(ok ? 'ok' : 'fail')
    }

    try {
      await openBrowserExtensions(browser.id)
      setOpened(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '打开浏览器失败，请手动打开扩展管理页')
    }

    onStartWait?.()
  }

  const copyDir = async () => {
    const dir = activeDir || dirFor('chromium')
    if (!dir) return
    setCopied((await copyText(dir)) ? 'ok' : 'fail')
  }

  const openFolder = async (family: string) => {
    setError('')
    try {
      await openExtensionFolder(family)
    } catch (err) {
      setError(err instanceof Error ? err.message : '打开扩展目录失败')
    }
  }

  const family = active?.family || 'chromium'
  const isOnboarding = variant === 'onboarding'

  return (
    <div className={`ext-installer${isOnboarding ? ' is-onboarding' : ''}`}>
      <div className="ext-status" data-state={connected > 0 ? 'ok' : 'idle'}>
        <span className="ext-status-dot" aria-hidden />
        {connected > 0 ? (
          <span>
            已连接 <b>{connected}</b> 个浏览器插件，网页上的论文可以直接存入文献库
          </span>
        ) : (
          <span>还没有安装浏览器插件；下面选一个你常用的浏览器，按一步引导装好</span>
        )}
      </div>

      {loading ? (
        <p className="ext-hint">正在检测电脑上的浏览器…</p>
      ) : setup === null ? (
        <div className="ext-empty">
          <p>
            网页版无法直接读取本机浏览器。请在桌面客户端里打开
            <b> 设置 → 插件与连接 </b>
            完成安装，下面仍是完整的手动步骤。
          </p>
        </div>
      ) : browsers.length === 0 ? (
        <div className="ext-empty">
          <p>
            没有在常见安装位置找到 Chrome / Edge / Firefox。你仍可以手动安装：
            先打开浏览器的扩展管理页，再按下面的步骤加载扩展目录。
          </p>
          <button className="link-btn" onClick={() => void load()}>
            重新检测
          </button>
        </div>
      ) : (
        <div className="ext-browser-list">
          {browsers.map((browser) => {
            const selected = active?.id === browser.id
            return (
              <div className="ext-browser" data-selected={selected} key={browser.id}>
                <img src={ICONS[browser.id]} alt={browser.name} className="ext-browser-icon" />
                <div className="ext-browser-text">
                  <b>{browser.name}</b>
                  <span>{browser.family === 'firefox' ? 'Firefox 扩展' : '支持 Chrome / Edge 扩展格式'}</span>
                </div>
                <button
                  className={`btn ${selected ? 'ghost' : 'primary'} ext-browser-btn`}
                  onClick={() => void install(browser)}
                >
                  {selected && opened ? '已打开扩展页' : '一键安装'}
                </button>
              </div>
            )
          })}
        </div>
      )}

      {(active || setup) && (
        <div className="ext-guide">
          <div className="ext-guide-head">
            <b>
              {family === 'firefox'
                ? 'Firefox 安装步骤'
                : `${active?.name || 'Chrome / Edge'} 安装步骤`}
            </b>
            <span>{family === 'firefox' ? '临时载入，重启浏览器后需重新载入一次' : '装上后自动连接，不用配对码'}</span>
          </div>

          <p className="ext-note">
            {family === 'firefox'
              ? 'Firefox 不允许未签名扩展长期驻留，只能临时载入，所以需要你手动确认一次。扩展文件已经随安装包准备好。'
              : 'Chrome / Edge 不允许第三方程序静默安装扩展，这是浏览器的安全限制；扩展文件与路径已经随安装包准备好，只剩这一步由你点确认。'}
          </p>

          {family === 'firefox' ? (
            <ol className="ext-steps">
              <li>
                地址栏输入 <code>about:debugging#/runtime/this-firefox</code> 并回车
              </li>
              <li>点「临时载入附加组件」</li>
              <li>
                在文件选择框里进入下面的扩展目录，选中 <code>manifest.json</code> 打开
              </li>
              <li>回到 Paperead，插件会在几秒内自动连接</li>
            </ol>
          ) : (
            <ol className="ext-steps">
              <li>
                已为你打开该浏览器的扩展管理页；没打开就手动访问
                <code>{active?.extensions_url || 'chrome://extensions/'}</code>
              </li>
              <li>打开页面右上角的「开发者模式」开关</li>
              <li>点左上角「加载已解压的扩展程序」</li>
              <li>
                在弹窗顶部的地址栏按 <kbd>Ctrl</kbd>+<kbd>V</kbd> 粘贴下面的路径并回车，
                进入扩展文件夹后再点右下角「选择文件夹」
                （粘贴不进去时，点左侧文件夹逐级进入）
              </li>
              <li>回到 Paperead，插件会在几秒内自动连接</li>
            </ol>
          )}

          <p className="ext-hint">
            下面是扩展在电脑里的位置，不需要记住：点「复制路径」后直接粘贴到浏览器即可；
            也可以点「打开目录」从资源管理器查看。
          </p>

          <div className="ext-path">
            <code>{activeDir || '扩展目录在桌面客户端里自动准备'}</code>
            <button className="btn ghost ext-small-btn" onClick={() => void copyDir()}>
              {copied === 'ok' ? '已复制' : copied === 'fail' ? '复制失败' : '复制路径'}
            </button>
            <button
              className="btn ghost ext-small-btn"
              onClick={() => void openFolder(family)}
              disabled={!activeDir}
            >
              打开目录
            </button>
          </div>

          {copied === 'fail' && (
            <p className="ext-hint">
              剪贴板不可用，可以直接点「打开目录」后在文件选择框里选这个文件夹。
            </p>
          )}

          {family !== 'firefox' && (
            <p className="ext-note">
              浏览器可能提示「停用开发者模式运行的扩展程序」，关闭提示即可，扩展仍然可用。
              每次升级 Paperead 后扩展目录会自动更新，不用重新加载。
            </p>
          )}
          {family === 'firefox' && (
            <p className="ext-note">
              Firefox 要求扩展带官方签名才能长期保留；未签名扩展只能临时载入，
              关闭浏览器后需要按上面步骤重新载入。长期使用建议改用 Chrome 或 Edge。
            </p>
          )}
        </div>
      )}

      {error && <p className="ext-error">{error}</p>}

      <details className="ext-faq">
        <summary>装上后没有自动连接？</summary>
        <ul>
          <li>确认 Paperead 正在运行：插件只连本机，不经过网络或服务器。</li>
          <li>打开浏览器扩展管理页，确认 Paperead Connector 已启用、不是灰色的「已停用」。</li>
          <li>装完扩展后回到 Paperead 等 5 秒；仍在等待就先点一次「连接浏览器插件」。</li>
          <li>还是不行就重启浏览器，再打开一次 Paperead。</li>
        </ul>
        {onRefreshConnection && (
          <button className="link-btn" onClick={() => onRefreshConnection()}>
            重新检测连接状态
          </button>
        )}
      </details>
    </div>
  )
}