import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

/** 打印渲染宽度（px）：约等于 Letter 纸 165 dpi，字迹清楚且内存可控。 */
const PRINT_WIDTH = 1400

/** 论文标题 → 可保存的文件名。 */
export function pdfFilename(title: string): string {
  const name = title
    .replace(/[\\/:*?"<>|\r\n\t]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120)
  return `${name || 'paper'}.pdf`
}

/** 下载原版 PDF：先取回文件本体，再按论文标题保存（服务端文件名是论文 id）。 */
export async function downloadPdf(url: string, title: string): Promise<void> {
  const response = await fetch(url)
  if (!response.ok) throw new Error(`下载失败（HTTP ${response.status}）`)
  const href = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = href
  link.download = pdfFilename(title)
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(href), 10_000)
}

/**
 * 打印整份 PDF：用 pdf.js 逐页渲染后放进隐藏 iframe 调起打印。
 *
 * 直接 window.print() 在「原版」视图下只会打印当前可见的一页截图；
 * 这里渲染的是 PDF 的全部页面，打印 / 存为 PDF 得到的都是完整论文。
 */
export async function printPdf(
  url: string,
  onProgress?: (done: number, total: number) => void,
): Promise<void> {
  const doc = await pdfjsLib.getDocument({ url }).promise
  const frame = document.createElement('iframe')
  frame.setAttribute('aria-hidden', 'true')
  frame.tabIndex = -1
  // 不能 display:none / visibility:hidden，否则部分内核不会打印；挪到屏幕外即可
  frame.style.cssText =
    'position:fixed;left:-20000px;top:0;width:800px;height:1000px;border:0;opacity:0;pointer-events:none;'
  document.body.appendChild(frame)

  const frameDoc = frame.contentWindow?.document
  if (!frameDoc) {
    frame.remove()
    await doc.destroy()
    throw new Error('无法创建打印页面')
  }
  frameDoc.open()
  frameDoc.write(
    `<!doctype html><html><head><meta charset="utf-8"><style>
      @page { size: auto; margin: 0; }
      html, body { margin: 0; padding: 0; background: #fff; }
      /* 每张图占满一页，等比缩放；避免纸张比例和 PDF 页不一致时溢出产生空白页 */
      img { display: block; width: 100%; height: 100vh; object-fit: contain; break-after: page; }
      img:last-of-type { break-after: auto; }
    </style></head><body></body></html>`,
  )
  frameDoc.close()

  try {
    for (let pageNo = 1; pageNo <= doc.numPages; pageNo += 1) {
      const page = await doc.getPage(pageNo)
      const base = page.getViewport({ scale: 1 })
      const viewport = page.getViewport({ scale: PRINT_WIDTH / base.width })
      const canvas = document.createElement('canvas')
      canvas.width = Math.round(viewport.width)
      canvas.height = Math.round(viewport.height)
      const context = canvas.getContext('2d')
      if (!context) throw new Error('无法创建打印画布')
      await page.render({ canvas, viewport, intent: 'print' }).promise
      const image = document.createElement('img')
      image.src = canvas.toDataURL('image/png')
      frameDoc.body.appendChild(image)
      page.cleanup()
      canvas.width = 0
      canvas.height = 0
      onProgress?.(pageNo, doc.numPages)
    }
  } finally {
    await doc.destroy()
  }

  await Promise.all(
    Array.from(frameDoc.images).map(
      (image) =>
        new Promise<void>((resolve) => {
          if (image.complete) {
            resolve()
            return
          }
          image.onload = () => resolve()
          image.onerror = () => resolve()
        }),
    ),
  )

  const win = frame.contentWindow
  if (!win) {
    frame.remove()
    throw new Error('无法创建打印页面')
  }
  let cleaned = false
  const cleanup = () => {
    if (cleaned) return
    cleaned = true
    frame.remove()
  }
  win.addEventListener('afterprint', cleanup, { once: true })
  // 打印对话框长时间未关闭时兜底，避免隐藏 iframe 常驻内存
  window.setTimeout(cleanup, 120_000)
  win.focus()
  win.print()
}
