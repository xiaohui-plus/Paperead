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

/** 拼装给模型的材料：命中段落 + 同块后一段（承接“如下：”这类引导句）。 */
export function buildLocalContext(index: LocalIndex, hits: { doc: LocalDoc }[]): string {
  const orderMap = new Map(index.docs.map((doc) => [doc.order, doc]))
  const parts: string[] = []
  for (const { doc } of hits) {
    let body = doc.translated || doc.text
    const next = orderMap.get(doc.order + 1)
    if (next && next !== doc) {
      body = `${body}\n${next.translated || next.text}`
    }
    parts.push(`[P${doc.page}] ${body.slice(0, 900)}`)
  }
  return parts.join('\n\n')
}
