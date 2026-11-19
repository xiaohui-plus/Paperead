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
import { PluginNotice } from './PluginNotice'

const ICONS: Record<string, string> = {
  chrome: chromeIcon,
  edge: edgeIcon,
  brave: braveIcon,
  firefox: firefoxIcon,
}

const PAGES = ['选择浏览器', '加载扩展', '完成连接'] as const

interface Props {
  /** 设置页里嵌在面板中；onboarding 用于首次启动向导，分页展示。 */
  variant?: 'settings' | 'onboarding'
  connected: number
  /** 一键安装后开始等待扩展自动配对。 */
  onStartWait?: () => void
  /** 手动重新检测（父组件刷新插件列表）。 */
  onRefreshConnection?: () => void
  /** 分页向导最后一页的完成/关闭。 */
  onDone?: () => void
  /** 仅开发预览用：直接打开指定分页，便于截图回归。 */
  initialPage?: number
}

export function ExtensionInstaller({
  variant = 'settings',
  connected,
  onStartWait,
  onRefreshConnection,
  onDone,
  initialPage = 0,
}: Props) {
  const [setup, setSetup] = useState<ExtensionSetup | null>(null)
  const [loading, setLoading] = useState(true)
  const [active, setActive] = useState<BrowserInfo | null>(null)
  const [copied, setCopied] = useState<'idle' | 'ok' | 'fail'>('idle')
  const [opened, setOpened] = useState(false)
  const [error, setError] = useState('')
  const [page, setPage] = useState(() =>
    Math.min(PAGES.length - 1, Math.max(0, initialPage)),
  )

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

  if (isOnboarding) {
    return (
      <div className="ext-installer is-onboarding">
        <div className="ext-wizard-body">
          {page === 0 && (
            <div className="ext-page">
              {connected > 0 ? (
                <PluginNotice tone="ok" title={`已连接 ${connected} 个浏览器`}>
                  插件已经装好，可以直接关闭；也可以换一个浏览器再装一次。
                </PluginNotice>
              ) : (
                <PluginNotice tone="info" title="扩展已经随安装包放在电脑里">
                  不需要下载或解压。选一个你平时看论文用的浏览器，按三步装好。
                </PluginNotice>
              )}

              {loading ? (
                <PluginNotice tone="busy" title="正在检测浏览器…" />
              ) : setup === null ? (
                <PluginNotice tone="warn" title="当前环境读不到本机浏览器">
                  请在桌面客户端里完成安装；浏览器扩展文件就在安装包里。
                </PluginNotice>
              ) : browsers.length === 0 ? (
                <div className="ext-empty">
                  <p>没有在常见安装位置找到 Chrome / Edge / Firefox。你仍可以手动安装。</p>
                  <button className="link-btn" onClick={() => void load()}>
                    重新检测
                  </button>
                </div>
              ) : (
                <div className="ext-browser-list">
                  {browsers.map((browser) => {
                    const selected = active?.id === browser.id
                    return (
                      <button
                        type="button"
                        className="ext-browser"
                        data-selected={selected}
                        key={browser.id}
                        onClick={() => setActive(browser)}
                      >
                        <img src={ICONS[browser.id]} alt="" className="ext-browser-icon" />
                        <span className="ext-browser-text">
                          <b>{browser.name}</b>
                          <span>
                            {browser.family === 'firefox'
                              ? 'Firefox 扩展，需要每次启动后临时载入'
                              : 'Chrome / Edge 扩展格式'}
                          </span>
                        </span>
                        <span className="ext-browser-check" aria-hidden>
                          {selected ? '✓' : ''}
                        </span>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {page === 1 && (
            <div className="ext-page">
              <div className="ext-page-title">
                <h3>{active?.name || '浏览器'} 安装步骤</h3>
                <p>
                  {family === 'firefox'
                    ? '临时载入，重启浏览器后需要再载入一次。'
                    : '装上后会自动连接，不用配对码。'}
                </p>
              </div>
              <ol className="ext-steps">
                {family === 'firefox' ? (
                  <>
                    <li>
                      地址栏输入 <code>about:debugging#/runtime/this-firefox</code> 并回车
                    </li>
                    <li>点「临时载入附加组件」</li>
                    <li>
                      进入下面的扩展目录，选中 <code>manifest.json</code> 打开
                    </li>
                    <li>回到 Paperead，插件会在几秒内自动连接</li>
                  </>
                ) : (
                  <>
                    <li>
                      点下面的「打开扩展管理页」；没打开就手动访问
                      <code>{active?.extensions_url || 'chrome://extensions/'}</code>
                    </li>
                    <li>打开页面右上角的「开发者模式」</li>
                    <li>点左上角「加载已解压的扩展程序」</li>
                    <li>
                      在弹窗地址栏按 <kbd>Ctrl</kbd>+<kbd>V</kbd> 粘贴已复制的扩展路径并回车，
                      进入文件夹后点「选择文件夹」
                    </li>
                    <li>回到 Paperead，插件会在几秒内自动连接</li>
                  </>
                )}
              </ol>

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

              <div className="ext-page-actions">
                <button
                  className="btn primary"
                  onClick={() => active && void install(active)}
                  disabled={!active}
                >
                  {opened ? '重新打开扩展管理页' : '打开扩展管理页'}
                </button>
              </div>

              {copied === 'fail' && (
                <PluginNotice tone="warn" title="剪贴板不可用">
                  点「打开目录」后在文件选择框里手动进入这个文件夹。
                </PluginNotice>
              )}
              {error && (
                <PluginNotice tone="error" title="打开失败">
                  {error}
                </PluginNotice>
              )}
            </div>
          )}

          {page === 2 && (
            <div className="ext-page">
              {connected > 0 ? (
                <PluginNotice tone="ok" title={`已连接 ${connected} 个浏览器`}>
                  在网页上看到论文或 PDF，可以直接存进 Paperead。
                </PluginNotice>
              ) : (
                <PluginNotice tone="busy" title="等待扩展连接">
                  装好后通常几秒内自动连上。还没连上就点下面「重新检测」。
                </PluginNotice>
              )}
              <ul className="ext-check-list">
                <li>确认浏览器扩展页里 Paperead Connector 已启用，不是灰色的「已停用」。</li>
                <li>确认 Paperead 正开着：扩展只连本机，不经过网络或服务器。</li>
                <li>仍连不上就重启一次浏览器，再回来点「重新检测」。</li>
              </ul>
              {onRefreshConnection && (
                <div className="ext-page-actions">
                  <button className="btn ghost" onClick={() => onRefreshConnection()}>
                    重新检测连接状态
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="ext-pager">
          <div className="ext-dots" aria-label={`第 ${page + 1} 页，共 ${PAGES.length} 页`}>
            {PAGES.map((label, index) => (
              <span
                key={label}
                className="ext-dot"
                data-active={index === page}
                title={label}
              />
            ))}
            <span className="ext-pager-count">
              {page + 1} / {PAGES.length}
            </span>
          </div>
          <div className="ext-pager-actions">
            {page > 0 ? (
              <button className="btn ghost" onClick={() => setPage((value) => value - 1)}>
                上一步
              </button>
            ) : (
              <button className="btn ghost" onClick={onDone}>
                稍后再说
              </button>
            )}
            {page < PAGES.length - 1 ? (
              <button
                className="btn primary"
                onClick={() => setPage((value) => value + 1)}
                disabled={page === 0 && !active}
              >
                下一步
              </button>
            ) : (
              <button className="btn primary" onClick={onDone}>
                {connected > 0 ? '完成' : '完成并关闭'}
              </button>
            )}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="ext-installer">
      <PluginNotice tone={connected > 0 ? 'ok' : 'info'} title={connected > 0 ? '已连接' : '尚未安装'}>
        {connected > 0
          ? `已连接 ${connected} 个浏览器插件，网页上的论文可以直接存入文献库。`
          : '选一个你常用的浏览器，按一步引导装好。扩展随安装包内置，不需要下载。'}
      </PluginNotice>

      {loading ? (
        <PluginNotice tone="busy" title="正在检测电脑上的浏览器…" />
      ) : setup === null ? (
        <PluginNotice tone="warn" title="网页版无法读取本机浏览器">
          请在桌面客户端里打开「设置 → 插件与连接」完成安装，下面仍是完整的手动步骤。
        </PluginNotice>
      ) : browsers.length === 0 ? (
        <div className="ext-empty">
          <p>没有在常见安装位置找到 Chrome / Edge / Firefox。你仍可以手动安装。</p>
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
            <b>{family === 'firefox' ? 'Firefox 安装步骤' : `${active?.name || 'Chrome / Edge'} 安装步骤`}</b>
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
              </li>
              <li>回到 Paperead，插件会在几秒内自动连接</li>
            </ol>
          )}

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
            <PluginNotice tone="warn" title="剪贴板不可用">
              点「打开目录」后在文件选择框里选这个文件夹。
            </PluginNotice>
          )}

          {family !== 'firefox' ? (
            <p className="ext-note">
              浏览器可能提示「停用开发者模式运行的扩展程序」，关闭提示即可，扩展仍然可用。
              每次升级 Paperead 后扩展目录会自动更新，不用重新加载。
            </p>
          ) : (
            <p className="ext-note">
              Firefox 要求扩展带官方签名才能长期保留；未签名扩展只能临时载入，
              关闭浏览器后需要按上面步骤重新载入。长期使用建议改用 Chrome 或 Edge。
            </p>
          )}
        </div>
      )}

      {error && (
        <PluginNotice tone="error" title="操作失败">
          {error}
        </PluginNotice>
      )}

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