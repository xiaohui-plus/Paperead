// ==UserScript==
// @name         Paperead Connector
// @namespace    https://paperead.app/
// @version      0.1.0
// @description  把网页上的论文保存到本地 Paperead 文献库
// @author       Paperead
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_notification
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  'use strict'

  const PORTS = Array.from({ length: 10 }, (_, i) => 17321 + i)
  const MAX_BYTES = 100 * 1024 * 1024

  const getValue = (key, fallback) => {
    try {
      return GM_getValue(key, fallback)
    } catch {
      return fallback
    }
  }
  const setValue = (key, value) => {
    try {
      GM_setValue(key, value)
    } catch {
      /* 存储不可用时只影响本次会话 */
    }
  }
  const deleteValue = (key) => {
    try {
      GM_deleteValue(key)
    } catch {
      /* ignore */
    }
  }

  function request(options) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        timeout: 15000,
        ...options,
        onload: resolve,
        onerror: () => reject(new Error('网络请求失败')),
        ontimeout: () => reject(new Error('请求超时')),
      })
    })
  }

  async function discoverBase() {
    const cached = getValue('base', '')
    if (cached) {
      try {
        const res = await request({ method: 'GET', url: `${cached}/api/ext/v1/ping` })
        if (res.status === 200 && JSON.parse(res.responseText || '{}').app === 'paperead') {
          return cached
        }
      } catch {
        /* 缓存失效，重新探测 */
      }
    }
    for (const port of PORTS) {
      const base = `http://127.0.0.1:${port}`
      try {
        const res = await request({
          method: 'GET',
          url: `${base}/api/ext/v1/ping`,
          timeout: 300,
        })
        if (res.status === 200 && JSON.parse(res.responseText || '{}').app === 'paperead') {
          setValue('base', base)
          return base
        }
      } catch {
        /* 继续探测下一个端口 */
      }
    }
    throw new Error('Paperead 未启动，请先打开桌面端')
  }

  function token() {
    return getValue('token', '')
  }

  async function api(path, init = {}) {
    const base = await discoverBase()
    const headers = Object.assign({}, init.headers || {})
    headers.Authorization = `Bearer ${token()}`
    const res = await request({
      method: init.method || 'GET',
      url: `${base}${path}`,
      headers,
      data: init.body,
    })
    const data = (() => {
      try {
        return JSON.parse(res.responseText || '{}')
      } catch {
        return {}
      }
    })()
    if (res.status === 401) {
      deleteValue('token')
      throw new Error('未连接或授权已撤销，请重新连接')
    }
    if (res.status >= 400) {
      throw new Error(data.detail || `请求失败（HTTP ${res.status}）`)
    }
    return data
  }

  async function pair(code) {
    const base = await discoverBase()
    const res = await request({
      method: 'POST',
      url: `${base}/api/ext/v1/pair`,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ code: code || '', name: '用户脚本' }),
    })
    const data = JSON.parse(res.responseText || '{}')
    if (res.status >= 400 || !data.token) {
      throw new Error(data.detail || `配对失败（HTTP ${res.status}）`)
    }
    setValue('token', data.token)
    return data.token
  }

  /* ---------- 页面元数据 ---------- */

  const meta = (name) => {
    const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`)
    return (el && el.content ? el.content : '').trim()
  }
  const metas = (name) =>
    Array.from(document.querySelectorAll(`meta[name="${name}"]`))
      .map((el) => (el.content || '').trim())
      .filter(Boolean)

  function collect() {
    const href = location.href
    const arxiv = href.match(/arxiv\.org\/(?:abs|pdf)\/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?/i)
    const pubmed = href.match(/pubmed\.ncbi\.nlm\.nih\.gov\/(\d+)/i)
    const pmc = href.match(/pmc\.ncbi\.nlm\.nih\.gov\/articles\/(PMC\d+)/i)
    const isPdf = /\.pdf($|[?#])/i.test(href)

    let ld = null
    for (const script of Array.from(
      document.querySelectorAll('script[type="application/ld+json"]'),
    )) {
      try {
        const parsed = JSON.parse(script.textContent || '')
        const items = Array.isArray(parsed) ? parsed : [parsed]
        ld =
          items.find((item) =>
            String((item && item['@type']) || '').match(/ScholarlyArticle|Article/),
          ) || ld
      } catch {
        /* 单个 JSON-LD 损坏不影响其它元数据 */
      }
    }

    const doiMeta = meta('citation_doi')
    const doiMatch = (doiMeta || href || document.documentElement.innerHTML).match(
      /10\.\d{4,9}\/[^\s"'<>]+/,
    )
    const doi = (doiMeta || (doiMatch ? doiMatch[0] : '')).replace(/[.,;)\]]+$/, '')

    let source = 'web'
    let sourceId = ''
    if (arxiv) {
      source = 'arxiv'
      sourceId = `arxiv:${arxiv[1]}`
    } else if (pubmed) {
      source = 'pubmed'
      sourceId = `pubmed:${pubmed[1]}`
    } else if (pmc) {
      source = 'pmc'
      sourceId = `pmc:${pmc[1].toUpperCase()}`
    } else if (doi) {
      source = 'doi'
      sourceId = `doi:${doi.toLowerCase()}`
    }

    let authors = metas('citation_author').join(', ')
    if (!authors && ld && ld.author) {
      const author = Array.isArray(ld.author) ? ld.author : [ld.author]
      authors = author
        .map((item) => (typeof item === 'string' ? item : (item && item.name) || ''))
        .filter(Boolean)
        .join(', ')
    }

    const date = meta('citation_publication_date') || (ld && ld.datePublished) || ''
    const yearMatch = String(date).match(/(19|20)\d{2}/)
    const pdfLink = document.querySelector('link[type="application/pdf"]')
    const anchor = Array.from(document.querySelectorAll('a[href]')).find((a) =>
      /\.pdf($|[?#])/i.test(a.href || ''),
    )

    return {
      source,
      source_id: sourceId,
      title:
        meta('citation_title') ||
        (ld && ld.headline) ||
        meta('og:title') ||
        document.title ||
        'Untitled paper',
      authors,
      abstract: meta('citation_abstract') || (ld && ld.description) || meta('og:description'),
      doi,
      url: href,
      year: yearMatch ? Number(yearMatch[0]) : null,
      venue:
        meta('citation_journal_title') ||
        meta('citation_conference_title') ||
        (ld && ld.isPartOf && ld.isPartOf.name) ||
        '',
      pdf_url:
        meta('citation_pdf_url') ||
        (pdfLink && pdfLink.href) ||
        (arxiv ? `https://arxiv.org/pdf/${arxiv[1]}` : '') ||
        (anchor && anchor.href) ||
        (isPdf ? href.split('#')[0] : ''),
    }
  }

  const looksLikePaper = () =>
    Boolean(
      meta('citation_title') ||
        meta('citation_doi') ||
        /arxiv\.org\/(abs|pdf)\//i.test(location.href) ||
        /pubmed\.ncbi\.nlm\.nih\.gov\//i.test(location.href) ||
        /\.pdf($|[?#])/i.test(location.href),
    )

  /* ---------- 界面 ---------- */

  const host = document.createElement('div')
  host.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:2147483647'
  const root = host.attachShadow({ mode: 'open' })
  root.innerHTML = `
    <style>
      :host { all: initial; }
      .wrap { font: 13px/1.5 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: #17181a; }
      button { font: inherit; cursor: pointer; }
      .fab { width: 44px; height: 44px; border: 0; border-radius: 50%; background: #14508c; color: #fff; box-shadow: 0 6px 20px rgba(20,80,140,.35); font-weight: 700; }
      .panel { display: none; width: 300px; margin-bottom: 10px; padding: 14px; border-radius: 12px; background: #fff; box-shadow: 0 12px 36px rgba(0,0,0,.18); }
      .panel.open { display: block; }
      h3 { margin: 0 0 6px; font-size: 14px; }
      p { margin: 4px 0; font-size: 12px; color: #6b7280; }
      .title { color: #17181a; font-size: 13px; font-weight: 600; }
      select, input { width: 100%; box-sizing: border-box; height: 30px; margin-top: 6px; padding: 0 8px; border: 1px solid #dde5ef; border-radius: 7px; font: inherit; }
      .primary { width: 100%; height: 32px; margin-top: 10px; border: 0; border-radius: 7px; background: #14508c; color: #fff; }
      .primary:disabled { opacity: .55; cursor: default; }
      .link { border: 0; background: none; color: #14508c; padding: 0; text-decoration: underline; }
      .status { margin-top: 8px; padding: 6px 8px; border-radius: 6px; background: #f4f8fd; color: #2b3a4a; font-size: 12px; }
      .status.err { background: #fff1f0; color: #a61b1b; }
      .status.ok { background: #eef8f1; color: #1a6b38; }
      .row { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
    </style>
    <div class="wrap">
      <div class="panel" id="panel">
        <h3>保存到 Paperead</h3>
        <div id="body"><p>正在读取页面…</p></div>
        <div id="status" class="status" hidden></div>
      </div>
      <button class="fab" id="fab" title="Paperead">P</button>
    </div>
  `
  const panel = root.getElementById('panel')
  const body = root.getElementById('body')
  const statusEl = root.getElementById('status')
  const fab = root.getElementById('fab')

  const showStatus = (text, kind) => {
    statusEl.hidden = !text
    statusEl.textContent = text || ''
    statusEl.className = `status${kind ? ' ' + kind : ''}`
  }

  async function render() {
    if (!token()) {
      body.innerHTML = `
        <p>还没有连接 Paperead：先在 Paperead 设置 →「插件与连接」点「连接浏览器插件」，再点下面的按钮。</p>
        <button class="primary" id="connect">自动连接</button>
        <div class="row">
          <input id="code" placeholder="或输入 6 位配对码" inputmode="numeric" />
          <button class="link" id="manual">连接</button>
        </div>
      `
      body.querySelector('#connect').onclick = async (event) => {
        event.target.disabled = true
        try {
          await pair('')
          await render()
        } catch (error) {
          showStatus(error.message, 'err')
          event.target.disabled = false
        }
      }
      body.querySelector('#manual').onclick = async () => {
        const code = (body.querySelector('#code').value || '').trim()
        if (code.length !== 6) return showStatus('配对码应为 6 位数字', 'err')
        try {
          await pair(code)
          await render()
        } catch (error) {
          showStatus(error.message, 'err')
        }
      }
      return
    }

    const info = collect()
    body.innerHTML = `
      <div class="title" id="title"></div>
      <p id="sub"></p>
      <select id="folder"><option value="">全部文献</option></select>
      <button class="primary" id="save">保存到 Paperead</button>
    `
    body.querySelector('#title').textContent = info.title
    body.querySelector('#sub').textContent = [info.authors, info.year, info.venue, info.doi]
      .filter(Boolean)
      .join(' · ')

    const folderSelect = body.querySelector('#folder')
    try {
      const data = await api('/api/ext/v1/folders')
      for (const folder of data.folders || []) {
        const option = document.createElement('option')
        option.value = folder.id
        option.textContent = `${folder.name}（${folder.count}）`
        folderSelect.appendChild(option)
      }
    } catch {
      /* 文件夹加载失败不影响保存 */
    }

    body.querySelector('#save').onclick = async (event) => {
      const button = event.target
      button.disabled = true
      try {
        await save(info, folderSelect.value)
      } catch (error) {
        showStatus(error.message, 'err')
        button.disabled = false
      }
    }
  }

  const isPdfBytes = (bytes) =>
    bytes.length > 4 &&
    bytes[0] === 0x25 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x44 &&
    bytes[3] === 0x46

  async function fetchPdf(url) {
    if (!url) throw new Error('没有找到 PDF 地址')
    try {
      const response = await fetch(url, { credentials: 'include' })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const bytes = new Uint8Array(await response.arrayBuffer())
      if (!isPdfBytes(bytes)) throw new Error('not-pdf')
      if (bytes.length > MAX_BYTES) throw new Error('PDF 超过 100MB')
      return bytes
    } catch (error) {
      if (error.message === 'PDF 超过 100MB') throw error
      const res = await request({ method: 'GET', url, responseType: 'arraybuffer' })
      if (res.status >= 400) throw new Error('该 PDF 需要登录或验证，请下载后拖入 Paperead')
      const bytes = new Uint8Array(res.response)
      if (!isPdfBytes(bytes)) {
        throw new Error('该 PDF 需要登录或验证，请下载后拖入 Paperead')
      }
      if (bytes.length > MAX_BYTES) throw new Error('PDF 超过 100MB')
      return bytes
    }
  }

  async function save(info, folderId) {
    showStatus('正在检查文献库…')
    const params = new URLSearchParams()
    if (info.source_id) params.set('source_id', info.source_id)
    if (info.doi) params.set('doi', info.doi)
    if (info.url) params.set('url', info.url)
    const existing = await api(`/api/ext/v1/papers/lookup?${params.toString()}`)
    if (existing.found && existing.paper) {
      showStatus('已在文献库中')
      window.open(existing.paper.open_url || `${await discoverBase()}/?p=${existing.paper.id}`, '_blank')
      return
    }

    showStatus('正在获取 PDF…')
    const bytes = await fetchPdf(info.pdf_url)
    const form = new FormData()
    form.append('file', new File([bytes], 'paper.pdf', { type: 'application/pdf' }))
    form.append('metadata', JSON.stringify(info))
    if (folderId) form.append('folder_id', folderId)

    showStatus('正在上传…')
    const saved = await api('/api/ext/v1/papers/ingest', { method: 'POST', body: form })
    const paperId = saved.paper.id
    showStatus('正在解析…')

    for (let i = 0; i < 120; i += 1) {
      const current = await api(`/api/ext/v1/papers/${encodeURIComponent(paperId)}`)
      if (current.paper.status === 'ready') {
        showStatus('已存入文献库')
        notify('已存入 Paperead', info.title)
        window.open(current.open_url, '_blank')
        return
      }
      if (current.paper.status === 'failed') {
        showStatus('解析失败，可在文献库中重试', 'err')
        notify('解析失败', info.title)
        return
      }
      await new Promise((resolve) => setTimeout(resolve, 1500))
    }
    showStatus('解析时间较长，请到文献库查看', 'err')
  }

  function notify(title, message) {
    try {
      GM_notification({ title, text: message, timeout: 6000 })
    } catch {
      /* 通知不可用时忽略 */
    }
  }

  fab.onclick = () => {
    panel.classList.toggle('open')
    if (panel.classList.contains('open')) void render()
  }

  if (looksLikePaper()) {
    document.documentElement.appendChild(host)
  }
})()
