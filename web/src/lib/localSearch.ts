/**
 * 本机论文检索：移植自 `paperead/tutor/qa.py`，供手机离线问答使用。
 *
 * 只索引正文段落，BM25 中英混排；同一页最多取 2 段，避免出处集中在一页。
 */
import type { Paper } from '../types'

const STOP = new Set([
  'the', 'a', 'an', 'of', 'and', 'or', 'to', 'in', 'for', 'on', 'with', 'is',
  'are', 'was', 'were', 'be', 'been', 'this', 'that', 'these', 'those', 'it',
  'as', 'by', 'at', 'from', 'we', 'our', 'can', 'may', 'not', 'but', 'which',
  '的', '了', '是', '在', '和', '与', '对', '把', '被', '这', '那', '有', '也',
])

const NOISE_SECTION_RE =
  /acknowledg|致\s*谢|author|作者|funding|基金|conflict|利益冲突|about the authors/i

const NOISE_RE =
  /开放获取|知识共享|creative commons|author biography|is (?:a|an) (?:professor|associate professor|phd|researcher|student)|研究领域|作者简介|通讯作者|基金项目|all rights reserved|downloaded from|教授|副教授|博士生导师|研究方向|E-?mail|邮箱|基金资助|(?:大学|学院|实验室|研究所).{0,20}(?:讲师|教授|研究员|博士)/i

export interface LocalDoc {
  blockId: string
  page: number
  order: number
  text: string
  translated: string
  tokens: string[]
}

class BM25 {
  private k1 = 1.4
  private b = 0.72
  private n: number
  private df = new Map<string, number>()
  private tf: Map<string, number>[] = []
  private len: number[] = []
  private avg = 0

  constructor(docs: string[][]) {
    this.n = docs.length
    for (const tokens of docs) {
      const counter = new Map<string, number>()
      for (const token of tokens) {
        counter.set(token, (counter.get(token) ?? 0) + 1)
      }
      this.tf.push(counter)
      this.len.push(tokens.length)
      for (const token of counter.keys()) {
        this.df.set(token, (this.df.get(token) ?? 0) + 1)
      }
    }
    this.avg = this.n ? this.len.reduce((sum, n) => sum + n, 0) / this.n : 0
  }

  score(query: string[]): number[] {
    const out = new Array<number>(this.n).fill(0)
    if (!this.n || !query.length) return out
    for (const term of new Set(query)) {
      const df = this.df.get(term) ?? 0
      if (!df) continue
      const idf = Math.log(1 + (this.n - df + 0.5) / (df + 0.5))
      for (let i = 0; i < this.n; i += 1) {
        const f = this.tf[i].get(term) ?? 0
        if (!f) continue
        const dl = this.len[i] || 1
        out[i] +=
          idf *
          ((f * (this.k1 + 1)) /
            (f + this.k1 * (1 - this.b + (this.b * dl) / (this.avg || 1))))
      }
    }
    return out
  }
}

export function tokenize(text: string): string[] {
  const value = text || ''
  const tokens = (value.match(/[A-Za-z][A-Za-z-]{1,}/g) ?? [])
    .map((token) => token.toLowerCase())
    .filter((token) => !STOP.has(token))

  // 中文二元组：单字匹配会把“章”“文”算成相关
  for (const run of value.match(/[\u4e00-\u9fa5]+/g) ?? []) {
    if (run.length === 1) {
      tokens.push(run)
      continue
    }
    for (let i = 0; i < run.length - 1; i += 1) {
      const bigram = run.slice(i, i + 2)
      if (!STOP.has(bigram)) tokens.push(bigram)
    }
  }
  return tokens
}

export interface LocalIndex {
  docs: LocalDoc[]
  en: BM25
  zh: BM25
}

export function buildLocalIndex(paper: Paper): LocalIndex {
  const docs: LocalDoc[] = []
  for (const block of paper.blocks) {
    if (block.kind !== 'para') continue
    const body = block.text || block.translated || ''
    if (body.length < 120) continue
    if (NOISE_RE.test(body) || (block.translated && NOISE_RE.test(block.translated))) {
      continue
    }
    if (NOISE_SECTION_RE.test(block.section || '')) continue
    docs.push({
      blockId: block.id,
      page: block.page,
      order: block.order,
      text: block.text || '',
      translated: block.translated || '',
      tokens: [],
    })
  }
  return {
    docs,
    en: new BM25(docs.map((doc) => tokenize(doc.text))),
    zh: new BM25(docs.map((doc) => tokenize(doc.translated))),
  }
}

export function retrieveLocal(
  index: LocalIndex,
  query: string,
  k = 6,
): { doc: LocalDoc; score: number }[] {
  const q = tokenize(query)
  const sEn = index.en.score(q)
  const hasChinese = /[\u4e00-\u9fa5]/.test(query)
  const sZh = hasChinese ? index.zh.score(q) : new Array<number>(index.docs.length).fill(0)
  const merged = index.docs.map((doc, i) => ({
    doc,
    score: Math.max(sEn[i], sZh[i]),
  }))
  merged.sort((a, b) => b.score - a.score)

  const out: { doc: LocalDoc; score: number }[] = []
  const perPage = new Map<number, number>()
  for (const hit of merged) {
    if (hit.score <= 0) break
    if ((perPage.get(hit.doc.page) ?? 0) >= 2) continue
    perPage.set(hit.doc.page, (perPage.get(hit.doc.page) ?? 0) + 1)
    out.push(hit)
    if (out.length >= k) break
  }
  return out
}


const DOSSIER_BUDGET = 26_000
const SKIP_KINDS = new Set(['margin', 'affiliation', 'author', 'title'])

function clipText(text: string, limit: number): string {
  const value = (text || '').replace(/\s+/g, ' ').trim()
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1)).trimEnd()}…`
}

function firstSentence(text: string, limit: number): string {
  const value = (text || '').replace(/\s+/g, ' ').trim()
  if (value.length <= limit) return value
  const cut = value.slice(0, limit)
  const match = [...cut.matchAll(/(?<=[。！？!?;；])|(?<=\.)\s+/g)]
  if (match.length && match[match.length - 1].index! > limit * 0.4) {
    return cut.slice(0, match[match.length - 1].index! + 1).trim()
  }
  return `${cut.trimEnd()}…`
}

export interface LocalDossier {
  text: string
  hits: { doc: LocalDoc; score: number }[]
}

/**
 * 本机版「论文档案」：一次把整篇论文拼成材料，超预算时保证每段至少出现首句，
 * 再用本地检索挑出重点段落展开全文。移植自 `paperead/tutor/dossier.py`。
 */
export function buildLocalDossier(paper: Paper, question: string): LocalDossier {
  const index = buildLocalIndex(paper)
  const hits = retrieveLocal(index, question)
  const ordered = [...paper.blocks].sort((a, b) => a.order - b.order)

  const outline: string[] = []
  const abstractParts: string[] = []
  let keywords = ''
  const figures: string[] = []
  const tables: string[] = []
  const equations: string[] = []
  const references: string[] = []
  const body: LocalDoc[] = []

  for (const block of ordered) {
    const kind = String(block.kind || '')
    if (SKIP_KINDS.has(kind)) continue
    const text = (block.text || '').trim()
    const translated = (block.translated || '').trim()
    if (!text && !translated && !block.caption) continue

    if (kind === 'heading') {
      if (text) outline.push(`- ${clipText(text, 60)}（第 ${block.page} 页）`)
      continue
    }
    if (kind === 'abstract') {
      if (text) abstractParts.push(text)
      continue
    }
    if (kind === 'keywords') {
      keywords = clipText(`${text} ${translated}`.trim(), 300)
      continue
    }
    if (kind === 'figure' || kind === 'table') {
      const label = kind === 'figure' ? '图' : '表'
      const caption = clipText(block.caption || '', 140)
      const line = `- ${label}${caption ? `：${caption}` : ''}（第 ${block.page} 页）`
      ;(kind === 'figure' ? figures : tables).push(line)
      continue
    }
    if (kind === 'equation') {
      const tex = (block.latex || '').trim()
      if (tex) {
        const num = (block.eq_number || '').trim()
        equations.push(`- ${num ? `(${num}) ` : ''}$${clipText(tex, 200)}$`)
      }
      continue
    }
    if (kind === 'reference') {
      if (references.length < 20) references.push(`- ${clipText(text, 220)}`)
      continue
    }
    if (!['para', 'caption'].includes(kind)) continue
    if (!text && !translated) continue
    if (NOISE_RE.test(text)) continue
    body.push({
      blockId: block.id,
      page: block.page,
      order: block.order,
      text,
      translated,
      tokens: [],
    })
  }

  const head: string[] = [
    `【论文档案】《${paper.title || '未命名'}》 · 共 ${paper.page_count || '?'} 页 · 正文 ${body.length} 段`,
  ]
  if (outline.length) head.push(`【章节大纲】\n${outline.slice(0, 40).join('\n')}`)
  if (keywords) head.push(`【关键词】${keywords}`)
  if (abstractParts.length) head.push(`【摘要】\n${clipText(abstractParts.join('\n'), 1_600)}`)
  if (figures.length) head.push(`【图表清单】\n${figures.slice(0, 24).join('\n')}`)
  if (tables.length) head.push(`【表格清单】\n${tables.slice(0, 16).join('\n')}`)
  if (equations.length) head.push(`【主要公式】\n${equations.slice(0, 12).join('\n')}`)
  if (references.length) {
    head.push(`【参考文献（节选，仅用于核对编号）】\n${references.join('\n')}`)
  }

  const headText = head.join('\n\n')
  const budget = Math.max(2_000, DOSSIER_BUDGET - headText.length - 800)
  const full = body.filter((doc) => doc.text)
  const total = full.reduce((sum, doc) => sum + doc.text.length, 0)
  const rendered: string[] = []

  if (total <= budget) {
    rendered.push('【全篇正文（全文）】按页顺序排列，可直接引用。')
    for (const doc of full) rendered.push(`[P${doc.page}] ${doc.text}`)
  } else {
    rendered.push(
      '【全篇正文（节选模式）】为保证整篇论文都在材料里，下面每段至少列出首句；' +
        '标注「全文」的是与本次问题最相关的段落。未标「全文」的段落如需细节，' +
        '请说明现有材料只到首句，不要臆测展开。',
    )
    let briefBudget = Math.floor(budget * 0.55)
    let focusBudget = budget - briefBudget
    const per = Math.max(90, Math.min(220, Math.floor(briefBudget / Math.max(1, full.length))))
    briefBudget -= per
    for (const doc of full) {
      if (briefBudget <= 0) {
        rendered.push(`[P${doc.page}] （省略：受篇幅限制）`)
        continue
      }
      const snippet = firstSentence(doc.text, per)
      rendered.push(`[P${doc.page}] ${snippet}`)
      briefBudget -= snippet.length
    }
    const byId = new Map(full.map((doc) => [doc.blockId, doc]))
    const expanded = new Set<string>()
    for (const hit of hits) {
      if (focusBudget <= 0) break
      const doc = byId.get(hit.doc.blockId)
      if (!doc || expanded.has(doc.blockId)) continue
      expanded.add(doc.blockId)
      const part = clipText(doc.text, Math.min(1_400, focusBudget))
      if (!part) continue
      rendered.push(`[P${doc.page} · 全文] ${part}`)
      focusBudget -= part.length
    }
  }

  return { text: `${headText}\n\n${rendered.join('\n\n')}`, hits }
}

/** 历史对话按距离压缩：最近两轮完整，越远越短（与后端 history_curve 一致）。 */
export function historyCurve(rows: { question: string; answer: string }[]): string {
  if (!rows.length) return ''
  const lines: string[] = ['【历史对话·越近越完整】']
  let budget = 3_600
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const distance = rows.length - i
    if (budget <= 0) {
      lines.push('- …（更早的对话已省略）')
      break
    }
    const [qLimit, aLimit] = distance <= 2 ? [600, 700] : distance <= 5 ? [320, 320] : [160, 120]
    const row = rows[i]
    let line = `- 问：${clipText(row.question, qLimit)}`
    if (row.answer && aLimit > 0) line += `\n  答：${clipText(row.answer, aLimit)}`
    if (distance > 3) line += '\n  （较早，细节可能已模糊）'
    lines.push(line)
    budget -= line.length
  }
  lines.push('（最近一轮最可信；更早的内容只作背景，不作为论文事实。）')
  return lines.join('\n')
}
