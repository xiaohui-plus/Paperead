import { browser } from 'wxt/browser'
import { defineContentScript } from 'wxt/utils/define-content-script'
import { extractMetadataInPage } from '../lib/metadata'
import type { PaperSummary } from '../lib/types'

const SEVEN_DAYS = 7 * 24 * 60 * 60 * 1000
const HOST_ID = 'paperead-pet-host'

interface SaveResponse {
  paper?: PaperSummary
  error?: string
}

function citationTitle(): string {
  const element = document.querySelector(
    'meta[name="citation_title"], meta[property="citation_title"]',
  ) as HTMLMetaElement | null
  return (element?.content || '').trim()
}

/** 只有带 DOI / arXiv / PubMed / PMC 或 citation_title 的页面才算论文页。 */
function looksLikePaper(): boolean {
  try {
    const meta = extractMetadataInPage(location.href)
    return Boolean(meta.doi || meta.source_id || citationTitle())
  } catch {
    return false
  }
}

function errorText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : '操作失败，请稍后重试'
}

function isContextInvalidated(error: unknown): boolean {
  return /Extension context invalidated|Receiving end does not exist|Could not establish connection/i.test(
    errorText(error),
  )
}

function runtimeAlive(): boolean {
  try {
    return Boolean(browser.runtime?.id)
  } catch {
    return false
  }
}

async function sendMessage(
  message: unknown,
): Promise<{ response?: unknown; error?: string; invalidated: boolean }> {
  if (!runtimeAlive()) return { invalidated: true }
  try {
    return {
      response: await browser.runtime.sendMessage(message),
      invalidated: false,
    }
  } catch (error) {
    if (isContextInvalidated(error)) return { invalidated: true }
    return { invalidated: false, error: errorText(error) }
  }
}

const STYLE = `
:host { all: initial; }
.wrap {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 2147483646;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Microsoft YaHei", sans-serif;
}
.pet {
  position: relative;
  width: 48px;
  height: 48px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: #1f2328;
  box-shadow: 0 6px 18px rgba(15, 23, 42, 0.28);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  cursor: pointer;
  animation: breathe 4s ease-in-out infinite;
  transition: transform 0.16s ease;
}
.pet:hover { transform: scale(1.04); }
.pet:focus-visible { outline: 2px solid #7aa7d9; outline-offset: 2px; }
.eye {
  width: 7px;
  height: 9px;
  border-radius: 50%;
  background: #eef4ff;
  display: block;
}
.dot {
  position: absolute;
  right: 2px;
  bottom: 2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #22c55e;
  border: 2px solid #1f2328;
  box-sizing: border-box;
}
.panel {
  position: absolute;
  right: 0;
  bottom: 58px;
  width: 190px;
  padding: 10px;
  border-radius: 12px;
  background: #1f2328;
  color: #f6f8fa;
  box-shadow: 0 10px 30px rgba(15, 23, 42, 0.32);
  display: flex;
  flex-direction: column;
  gap: 6px;
  opacity: 0;
  visibility: hidden;
  transform: translateY(6px);
  pointer-events: none;
  transition: opacity 0.16s ease, transform 0.16s ease, visibility 0.16s;
}
.wrap:hover .panel,
.wrap.open .panel {
  opacity: 1;
  visibility: visible;
  transform: translateY(0);
  pointer-events: auto;
}
.act {
  width: 100%;
  padding: 7px 9px;
  border: 0;
  border-radius: 7px;
  background: #f6f8fa;
  color: #1f2328;
  font: inherit;
  font-size: 12px;
  line-height: 1.4;
  text-align: left;
  cursor: pointer;
}
.act:hover { background: #ffffff; }
.act:disabled { opacity: 0.6; cursor: default; }
.msg {
  margin: 0;
  font-size: 11px;
  line-height: 1.45;
  color: #c7cdd4;
  word-break: break-word;
}
.msg:empty { display: none; }
.close {
  position: absolute;
  top: 2px;
  right: 4px;
  width: 16px;
  height: 16px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: #9aa1ac;
  font: inherit;
  font-size: 12px;
  line-height: 16px;
  cursor: pointer;
}
.close:hover { color: #ffffff; }
@keyframes breathe {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.05); }
}
@media (prefers-reduced-motion: reduce) {
  .pet { animation: none; transition: none; }
  .panel { transition: none; }
}
`

function mount(dismissKey: string): void {
  if (document.getElementById(HOST_ID)) return

  const host = document.createElement('div')
  host.id = HOST_ID
  const shadow = host.attachShadow({ mode: 'open' })
  const style = document.createElement('style')
  style.textContent = STYLE
  const wrap = document.createElement('div')
  wrap.className = 'wrap'
  wrap.innerHTML = `
    <div class="panel" role="group" aria-label="Paperead 快捷操作">
      <button class="close" type="button" aria-label="关闭灵宠" title="7 天内不再显示">×</button>
      <button class="act save" type="button">保存到 Paperead</button>
      <button class="act assist" type="button">打开阅读助手</button>
      <p class="msg" role="status"></p>
    </div>
    <button class="pet" type="button" aria-label="Paperead 灵宠" aria-expanded="false">
      <span class="eye"></span><span class="eye"></span><span class="dot"></span>
    </button>
  `
  shadow.append(style, wrap)
  document.documentElement.appendChild(host)

  const pet = wrap.querySelector('.pet') as HTMLButtonElement
  const panel = wrap.querySelector('.panel') as HTMLDivElement
  const closeButton = wrap.querySelector('.close') as HTMLButtonElement
  const saveButton = wrap.querySelector('.save') as HTMLButtonElement
  const assistButton = wrap.querySelector('.assist') as HTMLButtonElement
  const message = wrap.querySelector('.msg') as HTMLParagraphElement

  let savedPaper: PaperSummary | null = null
  let saving = false

  const setMessage = (text: string) => {
    message.textContent = text
  }

  const setOpen = (open: boolean) => {
    wrap.classList.toggle('open', open)
    pet.setAttribute('aria-expanded', String(open))
  }

  pet.addEventListener('click', (event) => {
    event.stopPropagation()
    setOpen(!wrap.classList.contains('open'))
  })

  const dismiss = () => {
    host.remove()
    try {
      void browser.storage.local
        .set({ [dismissKey]: Date.now() })
        .catch(() => {})
    } catch {
      // 扩展上下文失效时忽略
    }
  }

  closeButton.addEventListener('click', (event) => {
    event.stopPropagation()
    dismiss()
  })

  const openReadingPage = async () => {
    try {
      const openUrl = typeof savedPaper?.open_url === 'string' ? savedPaper.open_url : ''
      const target =
        openUrl ||
        browser.runtime.getURL(`/viewer.html?file=${encodeURIComponent(location.href)}`)
      await browser.tabs.create({ url: target })
    } catch (error) {
      if (!isContextInvalidated(error)) setMessage(errorText(error))
    }
  }

  saveButton.addEventListener('click', async () => {
    if (saving) return
    if (savedPaper) {
      await openReadingPage()
      return
    }
    saving = true
    saveButton.disabled = true
    setMessage('正在保存…')
    try {
      const result = await sendMessage({ type: 'save-tab' })
      if (result.invalidated) {
        setMessage('')
        return
      }
      if (result.error) {
        setMessage(result.error)
        return
      }
      const data = result.response as SaveResponse | undefined
      if (data?.error) {
        setMessage(data.error)
        return
      }
      const paper = data?.paper
      if (!paper) {
        setMessage('没有识别到论文信息')
        return
      }
      savedPaper = paper
      saveButton.textContent = '已保存 · 打开阅读页'
      if (paper.status === 'failed') {
        setMessage('已存入，解析失败可到文献库重试')
      } else if (paper.status === 'ready') {
        setMessage('已保存')
      } else {
        setMessage('已存入，正在解析…')
      }
    } finally {
      saving = false
      saveButton.disabled = false
    }
  })

  assistButton.addEventListener('click', async () => {
    const result = await sendMessage({ type: 'open-assistant' })
    if (result.invalidated) return
    const data = result.response as { ok?: boolean } | undefined
    if (result.error || !data?.ok) {
      setMessage('请点工具栏的 P 图标打开')
      return
    }
    setMessage('已在侧边栏打开')
  })
}

export default defineContentScript({
  matches: ['http://*/*', 'https://*/*'],
  runAt: 'document_idle',
  main: async () => {
    if (window.top !== window.self) return
    if (!looksLikePaper()) return

    const dismissKey = `petDismissed:${location.hostname}`
    try {
      const stored = await browser.storage.local.get(dismissKey)
      const dismissedAt = Number(stored[dismissKey] || 0)
      if (dismissedAt && Date.now() - dismissedAt < SEVEN_DAYS) return
    } catch {
      // 读不到记录时照常显示
    }
    mount(dismissKey)
  },
})
