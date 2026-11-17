import { browser } from 'wxt/browser'
import { defineBackground } from 'wxt/utils/define-background'
import { acquirePdf } from '../lib/pdf'
import { getPaper, ingest, isPaired, lookup } from '../lib/api'
import { extractMetadataInPage } from '../lib/metadata'
import type { PaperMetadata, TaskState } from '../lib/types'

const MENU_SAVE_PAGE = 'save-page'
const MENU_SAVE_LINK = 'save-link'

function isPdfUrl(url: string): boolean {
  return /\.pdf($|[?#])/i.test(url)
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

async function pollUntilDone(paperId: string, task: TaskState): Promise<void> {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const paper = await getPaper(paperId)
    if (paper.status === 'ready') {
      await setTask({
        ...task,
        status: 'ready',
        message: '已存入文献库',
        paperId,
        openUrl: (paper.open_url as string) || '',
      })
      await notify('已存入 Paperead', task.title)
      return
    }
    if (paper.status === 'failed') {
      await setTask({
        ...task,
        status: 'failed',
        message: '解析失败，可在文献库中重试',
        paperId,
      })
      await notify('解析失败', task.title)
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 1500))
  }
  await setTask({
    ...task,
    status: 'failed',
    message: '解析时间较长，请到文献库查看',
    paperId,
  })
}

async function saveTab(
  tabId: number,
  folderId = '',
  pdfUrlOverride = '',
): Promise<TaskState> {
  if (!(await isPaired())) {
    throw new Error('请先在扩展弹窗中完成配对')
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

  const existing = await lookup(meta)
  if (existing.found && existing.paper) {
    await setTask({
      ...task,
      status: 'existing',
      message: '已在文献库中',
      paperId: existing.paper.id,
      openUrl: existing.paper.open_url,
    })
    return task
  }

  const bytes = await acquirePdf(meta, tabId)
  await setTask({ ...task, message: '正在上传' })
  const saved = await ingest(bytes, meta, folderId)
  await setTask({
    ...task,
    status: 'working',
    message: '正在解析',
    paperId: saved.paper.id,
    openUrl: saved.paper.open_url,
  })
  await pollUntilDone(saved.paper.id, task)
  return task
}

export default defineBackground(() => {
  browser.runtime.onInstalled.addListener(() => {
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

  browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
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
    return false
  })
})
