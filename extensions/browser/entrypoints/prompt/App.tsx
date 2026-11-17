import { useMemo, useState } from 'react'
import { browser } from 'wxt/browser'

type Choice = 'paperead' | 'native'

function fileNameOf(url: string): string {
  try {
    const last = new URL(url).pathname.split('/').filter(Boolean).pop() || 'document.pdf'
    return decodeURIComponent(last)
  } catch {
    return 'document.pdf'
  }
}

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return ''
  }
}

export default function App() {
  const params = useMemo(() => new URLSearchParams(location.search), [])
  const file = params.get('file') || ''
  const host = hostOf(file)
  const name = fileNameOf(file)
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState(false)

  const choose = async (kind: Choice) => {
    if (!file || busy) return
    setBusy(true)
    try {
      if (remember) {
        await browser.storage.local.set({ [`pdfChoice:${host}`]: kind })
      } else if (kind === 'native') {
        await browser.storage.session.set({ [`pdfBypass:${file}`]: true })
      }
      const target =
        kind === 'paperead'
          ? browser.runtime.getURL(
              ('/viewer.html?file=' + encodeURIComponent(file)) as Parameters<
                typeof browser.runtime.getURL
              >[0],
            )
          : file
      location.replace(target)
    } catch {
      setBusy(false)
    }
  }

  return (
    <main className="page">
      <section className="card">
        <div className="pet" aria-hidden="true">
          <span className="eye" />
          <span className="eye" />
          <span className="dot" />
        </div>
        <h1>检测到 PDF</h1>
        <p className="file" title={name}>
          {name}
        </p>
        {host && <p className="host">{host}</p>}
        <p className="desc">
          用 Paperead 打开会自动入库并启用翻译、问答和批注；也可以继续用浏览器自带的 PDF 查看器。
        </p>
        <button className="primary" type="button" disabled={busy || !file} onClick={() => void choose('paperead')}>
          用 Paperead 阅读
        </button>
        <button className="secondary" type="button" disabled={busy || !file} onClick={() => void choose('native')}>
          用浏览器打开
        </button>
        <label className="remember">
          <input
            type="checkbox"
            checked={remember}
            onChange={(event) => setRemember(event.target.checked)}
          />
          记住这个站点的选择
        </label>
        <p className="hint">选择只保存在本机浏览器；单篇 PDF 仍可在侧边栏里改用 Paperead 打开。</p>
      </section>
    </main>
  )
}
