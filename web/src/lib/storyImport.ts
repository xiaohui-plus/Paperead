/**
 * 从手机端 StoryReader 导入。
 *
 * StoryReader 的导出格式没有公开规范，这里做宽松解析：
 * - JSON：`{books:[...]}` / `[...]` / `{title, chapters:[...]}`，
 *   章节内容支持 `content/text/body/html/paragraphs`；
 * - TXT / Markdown：按「第X章」「# 标题」「Chapter N」切章。
 *
 * 导入结果写入本机离线库，阅读、翻译、问答都走本机引擎。
 */
import type { Block } from '../types'
import { saveLocalDocument, type OfflineSummary } from './offlineStore'

interface Chapter {
  title: string
  content: string
}

interface RawBook {
  title?: unknown
  name?: unknown
  bookName?: unknown
  author?: unknown
  writer?: unknown
  chapters?: unknown
  blocks?: unknown
  items?: unknown
}

const HTML_TAG = /<[^>]+>/g

function cleanText(value: unknown): string {
  return String(value ?? '')
    .replace(HTML_TAG, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .trim()
}

function pickString(record: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function chapterFrom(item: unknown, index: number): Chapter | null {
  if (typeof item === 'string') {
    return { title: `第 ${index + 1} 节`, content: item }
  }
  if (!item || typeof item !== 'object') return null
  const record = item as Record<string, unknown>
  const title =
    pickString(record, ['title', 'name', 'chapterName', 'chapter_title', 'heading']) ||
    `第 ${index + 1} 节`
  let content = pickString(record, ['content', 'text', 'body', 'html', 'chapterContent'])
  if (!content && Array.isArray(record.paragraphs)) {
    content = record.paragraphs.map((p) => cleanText(p)).filter(Boolean).join('\n\n')
  }
  return content.trim() ? { title, content: cleanText(content) } : null
}

function chaptersOf(book: RawBook): Chapter[] {
  const list = Array.isArray(book.chapters)
    ? book.chapters
    : Array.isArray(book.items)
      ? book.items
      : []
  return list
    .map((item, index) => chapterFrom(item, index))
    .filter((item): item is Chapter => item !== null)
}

function booksFromJson(raw: unknown): RawBook[] {
  if (Array.isArray(raw)) return raw as RawBook[]
  if (!raw || typeof raw !== 'object') return []
  const record = raw as Record<string, unknown>
  for (const key of ['books', 'bookList', 'library', 'items']) {
    if (Array.isArray(record[key])) return record[key] as RawBook[]
  }
  if (record.data && typeof record.data === 'object') {
    const nested = booksFromJson(record.data)
    if (nested.length) return nested
  }
  return [record as RawBook]
}

function splitChapterText(text: string): Chapter[] {
  const normalized = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim()
  if (!normalized) return []

  const lines = normalized.split('\n')
  const chapters: Chapter[] = []
  let title = ''
  let buffer: string[] = []

  const flush = () => {
    const content = buffer.join('\n').trim()
    if (!content && !title) return
    chapters.push({
      title: title || `第 ${chapters.length + 1} 节`,
      content,
    })
    buffer = []
  }

  const heading =
    /^(?:#{1,3}\s+.+|第\s*[0-9一二三四五六七八九十百千]+\s*[章节卷回篇].*|Chapter\s+\d+.*|CHAPTER\s+\d+.*)$/

  for (const line of lines) {
    if (heading.test(line.trim())) {
      if (title || buffer.join('').trim()) flush()
      title = line.replace(/^#{1,3}\s+/, '').trim()
    } else {
      buffer.push(line)
    }
  }
  flush()

  if (chapters.length <= 1) {
    return [{ title: title || '正文', content: normalized }]
  }
  return chapters
}

function paragraphsOf(content: string): string[] {
  const parts = content
    .split(/\n{2,}/)
    .map((part) => part.trim())
    .filter(Boolean)
  if (parts.length > 1) return parts
  // 硬换行的 TXT：按长度切段，避免整章变成一个超大块
  const out: string[] = []
  let rest = content.trim()
  while (rest.length > 1500) {
    const cut = Math.max(rest.lastIndexOf('。', 1500), rest.lastIndexOf('. ', 1500))
    const at = cut > 300 ? cut + 1 : 1500
    out.push(rest.slice(0, at).trim())
    rest = rest.slice(at).trim()
  }
  if (rest) out.push(rest)
  return out
}

function buildBlocks(docId: string, chapters: Chapter[]): Block[] {
  const blocks: Block[] = []
  let order = 0
  chapters.forEach((chapter, index) => {
    if (chapter.title) {
      blocks.push({
        id: `${docId}-h${order}`,
        order,
        page: index + 1,
        kind: 'heading',
        level: 1,
        section: chapter.title,
        text: chapter.title,
        translated: '',
        image_path: null,
        table_html: '',
        caption: '',
        translatable: false,
        bbox: [0, 0, 0, 0],
      })
      order += 1
    }
    for (const text of paragraphsOf(chapter.content)) {
      blocks.push({
        id: `${docId}-b${order}`,
        order,
        page: index + 1,
        kind: 'para',
        level: 0,
        section: chapter.title,
        text,
        translated: '',
        image_path: null,
        table_html: '',
        caption: '',
        translatable: true,
        bbox: [0, 0, 0, 0],
      })
      order += 1
    }
  })
  return blocks
}

function newId(): string {
  return `story-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

export async function importStoryFile(file: File): Promise<OfflineSummary> {
  const name = file.name.replace(/\.[^.]+$/, '')
  const lower = file.name.toLowerCase()

  let title = name
  let authors = ''
  let chapters: Chapter[] = []
  let blocks: Block[] | null = null

  if (lower.endsWith('.json')) {
    const raw = JSON.parse(await file.text()) as RawBook | RawBook[]
    const books = booksFromJson(raw)
    if (!books.length) throw new Error('没有读到任何书籍')
    // 一次导入多本书时，把每本都导入，返回最后一本用于提示
    let last: OfflineSummary | null = null
    for (const book of books) {
      last = await importOne(book, name)
    }
    return last as OfflineSummary
  }

  const text = await file.text()
  if (lower.endsWith('.txt') || lower.endsWith('.md') || lower.endsWith('.markdown')) {
    chapters = splitChapterText(text)
  } else {
    throw new Error('暂支持 StoryReader 导出的 JSON / TXT / Markdown')
  }

  const docId = newId()
  blocks = buildBlocks(docId, chapters)
  if (!blocks.length) throw new Error('文件里没有可读的正文')

  const payload = {
    paper_id: docId,
    title,
    title_zh: '',
    authors,
    affiliation: '',
    page_count: chapters.length,
    page_sizes: [],
    columns: 1,
    glossary: {},
    translation_lang: '',
    progress: { pct: 0, block_id: '', page: 1 },
    blocks,
  }
  return saveLocalDocument(docId, payload)
}

async function importOne(book: RawBook, fallbackTitle: string): Promise<OfflineSummary> {
  const title =
    pickString(book as Record<string, unknown>, ['title', 'name', 'bookName']) || fallbackTitle
  const authors = pickString(book as Record<string, unknown>, ['author', 'writer'])

  // 已经是我们自己的 blocks 形状时直接沿用
  if (Array.isArray(book.blocks)) {
    const docId = newId()
    const blocks = book.blocks as Block[]
    const pages = Math.max(1, ...blocks.map((block) => Number(block.page) || 1))
    return saveLocalDocument(docId, {
      paper_id: docId,
      title,
      title_zh: '',
      authors,
      affiliation: '',
      page_count: pages,
      page_sizes: [],
      columns: 1,
      glossary: {},
      translation_lang: '',
      progress: { pct: 0, block_id: '', page: 1 },
      blocks,
    })
  }

  const chapters = chaptersOf(book)
  if (!chapters.length) throw new Error(`《${title}》没有章节内容`)
  const docId = newId()
  const blocks = buildBlocks(docId, chapters)
  return saveLocalDocument(docId, {
    paper_id: docId,
    title,
    title_zh: '',
    authors,
    affiliation: '',
    page_count: chapters.length,
    page_sizes: [],
    columns: 1,
    glossary: {},
    translation_lang: '',
    progress: { pct: 0, block_id: '', page: 1 },
    blocks,
  })
}
