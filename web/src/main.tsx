import React, { lazy, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import 'katex/dist/katex.min.css'
import './styles.css'
import './research/research.css'
import { migrateLegacyStorage } from './lib/storage'
import { syncVisionConfig } from './lib/settings'
import { syncResearchModel } from './lib/researchModel'
import { syncScholarConfig } from './lib/scholarConfig'
import { autoCheckUpdate, isAndroidApp, isDesktopApp } from './lib/update'
import { hasMobileServer, installMobileFetch, isMobileShell } from './lib/mobile'
import {
  pullModelSettings,
  restoreAppSettings,
  restoreModelSettings,
  startAppSettingsBackup,
} from './lib/settingsSync'
import { applyReadingFonts } from './lib/readingPrefs'

const TourRoot = lazy(() =>
  import('./tour/TourRoot').then((module) => ({ default: module.TourRoot })),
)

const mobile = isMobileShell()
if (mobile) {
  // Android 采用 edge-to-edge，给状态栏留出安全区
  document.documentElement.classList.add('mobile-shell')
}
// 安卓壳把相对 /api 请求转到用户配置的服务器，并附带访问口令
installMobileFetch()

if (mobile && hasMobileServer()) {
  // 连上电脑后自动同步模型配置；有变化就刷新一次，让组件读到新的服务商/密钥
  void pullModelSettings().then((changed) => {
    if (changed) window.location.reload()
  })
}

migrateLegacyStorage()
applyReadingFonts()

async function boot() {
  if (!mobile) {
    // 端口变化会让 localStorage 换命名空间；先补回数据目录里的整份设置
    await restoreAppSettings()
    await restoreModelSettings()
    startAppSettingsBackup()
  }
  if (!mobile || hasMobileServer()) {
    // 解析阶段识别公式需要识图模型配置；启动时先同步一次，避免要先打开设置页
    void syncVisionConfig()
    // 文献检索（Semantic Scholar / CORE）的 API Key 存在浏览器里，启动时同步给服务
    void syncScholarConfig()
  }
  if (!mobile) {
    // 研究 agent 通过本地代理调用模型，启动时把当前选择同步过去
    void syncResearchModel()
  }
  if (!mobile || isAndroidApp()) {
    // 每天自动检查一次新版本；桌面走 latest.json，安卓走原生 android.json
    void autoCheckUpdate()
  }

  createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <ErrorBoundary title="应用渲染失败" hint="刷新页面可以恢复；如果反复出现，请把这段错误信息反馈给我们。">
        <App />
        {isDesktopApp() && (
          <Suspense fallback={null}>
            <TourRoot />
          </Suspense>
        )}
      </ErrorBoundary>
    </React.StrictMode>,
  )
}

void boot()

// 滚动时给容器短暂加 is-scrolling，让全局滚动条只在滑动期间出现
const scrollTimers = new WeakMap<Element, number>()
document.addEventListener(
  'scroll',
  (event) => {
    const target = event.target
    const element =
      target === document
        ? document.documentElement
        : target instanceof Element
          ? target
          : null
    if (!element) return
    element.classList.add('is-scrolling')
    const timer = scrollTimers.get(element)
    if (timer) window.clearTimeout(timer)
    scrollTimers.set(
      element,
      window.setTimeout(() => {
        element.classList.remove('is-scrolling')
        scrollTimers.delete(element)
      }, 900),
    )
  },
  true,
)
