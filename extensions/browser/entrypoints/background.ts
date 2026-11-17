import { browser } from 'wxt/browser'
import { defineBackground } from 'wxt/utils/define-background'
import { acquirePdf } from '../lib/pdf'
import { getPaper, ingest, isPaired, lookup, pair } from '../lib/api'
import { extractMetadataInPage } from '../lib/metadata'
import type { PaperMetadata, PaperSummary, TaskState } from '../lib/types'

const MENU_SAVE_PAGE = 'save-page'
const MENU_SAVE_LINK = 'save-link'
const AUTO_PAIR_ALARM = 'paperead-auto-pair'

/** 后台自动认领配对窗口；没有窗口时静默跳过，下次 alarm 再试。 */
async function tryAutoPair(): Promise<void> {
  if (await isPaired()) return
  try {
    await pair('')
  } catch {
    // 桌面端未启动或未开等待窗口，保持静默。
  }
}

function isPdfUrl(url: string): boolean {
  return /\.pdf($|[?#])/i.test(url)
}

const PDF_CHOICE_PREFIX = 'pdfChoice:'
const PDF_BYPASS_PREFIX = 'pdfBypass:'

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return ''
  }
}

/**
 * PDF 不直接接管：第一次遇到某个站点时先给带灵宠的入口页，
 * 由用户选择「用 Paperead 阅读」或「用浏览器打开」，并按站点记住。
 */
async function handlePdfNavigation(tabId: number, url: string): Promise<void> {
  if (!isPdfUrl(url) || isRestrictedUrl(url)) return
  const host = hostOf(url)
  if (!host) return

  const bypassKey = PDF_BYPASS_PREFIX + url
  const bypass = await browser.storage.session.get(bypassKey)
  if (bypass[bypassKey]) {
    await browser.storage.session.remove(bypassKey)
    return
  }

  const choiceKey = PDF_CHOICE_PREFIX + host
  const stored = await browser.storage.local.get(choiceKey)
  const choice = stored[choiceKey]
  if (choice === 'native') return

  const target =
    choice === 'paperead'
      ? extensionUrl('/viewer.html?file=' + encodeURIComponent(url))
      : extensionUrl('/prompt.html?file=' + encodeURIComponent(url))
  await browser.tabs.update(tabId, { url: target })
}

function extensionUrl(path: string): string {
  return browser.runtime.getURL(path as Parameters<typeof browser.runtime.getURL>[0])
}

/** PDF 只做角标提示，不接管标签页。 */
function updatePdfBadge(tabId: number, url: string): void {
  try {
    const text = isPdfUrl(url) && !isRestrictedUrl(url) ? 'PDF' : ''
    void browser.action?.setBadgeText({ tabId, text }).catch(() => {})
  } catch {
    // 不支持 action API 的浏览器静默跳过
  }
}

/** chrome://、edge:// 等浏览器内部页面禁止扩展注入脚本 */
function isRestrictedUrl(url: string): boolean {
  return /^(chrome|edge|about|devtools|chrome-extension|moz-extension|file):/i.test(url)
}

async function setTask(task: TaskState): Promise<void> {
  const stored = await browser.storage.local.get('tasks')
  const tasks = (stored.tasks || {}) as Record<string, TaskState>
  tasks[task.id] = task
  await browser.storage.local.set({ tasks })
}

async function notify(title: string, message: string): Promise<void> {
  try {
    await browser.notifications.create({
      type: 'basic',
      iconUrl: browser.runtime.getURL('/icon/128.png'),
      title,
      message,
    })
  } catch {
    // 通知被系统关闭时不影响保存任务
  }
}

async function readMeta(tabId: number): Promise<PaperMetadata> {
  const tab = await browser.tabs.get(tabId)
  const url = tab.url || ''
  if (url && isRestrictedUrl(url) && !isPdfUrl(url)) {
    throw new Error('当前是浏览器内部页面，请切换到论文页面再保存')
  }
  if (isPdfUrl(url)) {
    return {
      source: 'pdf',
      source_id: '',
      title: decodeURIComponent(url.split('/').pop() || 'paper.pdf'),
      authors: '',
      abstract: '',
      doi: '',
      url,
      year: null,
      venue: '',
      pdf_url: url,
    }
  }
  const [injected] = await browser.scripting.executeScript({
    target: { tabId },
    func: extractMetadataInPage,
  })
  return injected!.result as PaperMetadata
}

async function waitForPaper(paper: PaperSummary): Promise<PaperSummary> {
  if (paper.status === 'ready' || paper.status === 'failed') return paper
  let current = paper
  for (let attempt = 0; attempt < 120; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1500))
    current = await getPaper(paper.id)
    if (current.status === 'ready' || current.status === 'failed') return current
  }
  return current
}

/**
 * 把当前标签页的论文送进文献库并等到解析结束。
 * 右键保存和侧边栏的 ensure-paper 共用这一条链路。
 */
async function preparePaper(
  meta: PaperMetadata,
  tabId: number,
  folderId: string,
  onStep: (message: string) => Promise<void>,
): Promise<{ paper: PaperSummary; existing: boolean }> {
  await onStep('正在检查文献库')
  const existing = await lookup(meta)
  if (existing.found && existing.paper) {
    return { paper: await waitForPaper(existing.paper), existing: true }
  }

  const bytes = await acquirePdf(meta, tabId)
  await onStep('正在上传')
  const saved = await ingest(bytes, meta, folderId)
  await onStep('正在解析')
  return { paper: await waitForPaper(saved.paper), existing: saved.already }
}

async function ensurePaper(tabId: number, folderId = ''): Promise<PaperSummary> {
  if (!(await isPaired())) {
    throw new Error('插件尚未连接 Paperead，请先打开桌面端')
  }
  const meta = await readMeta(tabId)
  const { paper } = await preparePaper(meta, tabId, folderId, async () => {})
  return paper
}

async function saveTab(
  tabId: number,
  folderId = '',
  pdfUrlOverride = '',
): Promise<TaskState> {
  if (!(await isPaired())) {
    throw new Error('请先在扩展侧边栏中完成配对')
  }
  const meta = await readMeta(tabId)
  if (pdfUrlOverride) meta.pdf_url = pdfUrlOverride

  const task: TaskState = {
    id: `${tabId}-${Date.now()}`,
    tabId,
    title: meta.title,
    status: 'working',
    message: '正在检查文献库',
  }
  await setTask(task)

  const { paper, existing } = await preparePaper(
    meta,
    tabId,
    folderId,
    async (message) => setTask({ ...task, message }),
  )

  if (paper.status === 'ready') {
    await setTask({
      ...task,
      status: existing ? 'existing' : 'ready',
      message: existing ? '已在文献库中' : '已存入文献库',
      paperId: paper.id,
      openUrl: paper.open_url || '',
    })
    if (!existing) await notify('已存入 Paperead', task.title)
  } else if (paper.status === 'failed') {
    await setTask({
      ...task,
      status: 'failed',
      message: '解析失败，可在文献库中重试',
      paperId: paper.id,
    })
    await notify('解析失败', task.title)
  } else {
    await setTask({
      ...task,
      status: 'failed',
      message: '解析时间较长，请到文献库查看',
      paperId: paper.id,
    })
  }
  return task
}

/** 点扩展图标直接打开阅读助手侧边栏；不支持的浏览器静默降级。 */
function setupSidePanel(): void {
  try {
    void browser.sidePanel?.setPanelBehavior({ openPanelOnActionClick: true }).catch(
      () => {},
    )
  } catch {
    // Firefox 等没有 sidePanel API
  }
}

export default defineBackground(() => {
  setupSidePanel()

  browser.runtime.onInstalled.addListener(() => {
    setupSidePanel()
    void browser.contextMenus.removeAll().then(() => {
      browser.contextMenus.create({
        id: MENU_SAVE_PAGE,
        title: '保存到 Paperead',
        contexts: ['page', 'selection'],
      })
      browser.contextMenus.create({
        id: MENU_SAVE_LINK,
        title: '保存链接指向的 PDF',
        contexts: ['link'],
      })
    })
  })

  void browser.alarms.create(AUTO_PAIR_ALARM, { periodInMinutes: 1 })
  browser.runtime.onStartup.addListener(() => {
    setupSidePanel()
    void tryAutoPair()
  })
  browser.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === AUTO_PAIR_ALARM) void tryAutoPair()
  })
  void tryAutoPair()

  browser.contextMenus.onClicked.addListener((info, tab) => {
    if (!tab?.id) return
    if (info.menuItemId === MENU_SAVE_PAGE) {
      void saveTab(tab.id).catch((error) =>
        notify('保存失败', error instanceof Error ? error.message : '未知错误'),
      )
    }
    if (info.menuItemId === MENU_SAVE_LINK && info.linkUrl) {
      void saveTab(tab.id, '', info.linkUrl).catch((error) =>
        notify('保存失败', error instanceof Error ? error.message : '未知错误'),
      )
    }
  })

  // 打开 PDF 时在扩展图标上提示「PDF」，并按站点选择决定是否进入入口页。
  browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.url === undefined && changeInfo.status !== 'complete') return
    const url = changeInfo.url || tab.url || ''
    updatePdfBadge(tabId, url)
    if (changeInfo.url) void handlePdfNavigation(tabId, changeInfo.url).catch(() => {})
  })

  browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
    const payload = message as {
      type: string
      tabId?: number
      folderId?: string
      pdfUrl?: string
    }
    if (payload.type === 'preview' && payload.tabId) {
      readMeta(payload.tabId)
        .then((meta) => sendResponse({ meta }))
        .catch((error) => sendResponse({ error: String(error.message || error) }))
      return true
    }
    if (payload.type === 'save' && payload.tabId) {
      saveTab(payload.tabId, payload.folderId, payload.pdfUrl)
        .then((task) => sendResponse({ task }))
        .catch((error) => sendResponse({ error: String(error.message || error) }))
      return true
    }
    if (payload.type === 'ensure-paper' && payload.tabId) {
      ensurePaper(payload.tabId)
        .then((paper) => sendResponse({ paper }))
        .catch((error) => sendResponse({ error: String(error.message || error) }))
      return true
    }
    // 浮动灵宠保存当前标签页，只信任 sender.tab
    if (payload.type === 'save-tab') {
      const tabId = sender.tab?.id
      if (typeof tabId !== 'number') {
        sendResponse({ error: '没有找到当前标签页' })
        return false
      }
      ensurePaper(tabId)
        .then((paper) => sendResponse({ paper }))
        .catch((error) => sendResponse({ error: String(error.message || error) }))
      return true
    }
    // 侧边栏只能在用户手势里打开；失败时让灵宠提示点工具栏图标
    if (payload.type === 'open-assistant') {
      const tabId = sender.tab?.id
      if (typeof tabId !== 'number' || !browser.sidePanel?.open) {
        sendResponse({ ok: false })
        return false
      }
      try {
        void browser.sidePanel
          .open({ tabId })
          .then(() => sendResponse({ ok: true }))
          .catch(() => sendResponse({ ok: false }))
      } catch {
        sendResponse({ ok: false })
      }
      return true
    }
    return false
  })
})
