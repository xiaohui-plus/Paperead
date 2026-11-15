export type BlockKind =
  | 'title'
  | 'author'
  | 'affiliation'
  | 'keywords'
  | 'heading'
  | 'para'
  | 'equation'
  | 'caption'
  | 'figure'
  | 'table'
  | 'reference'
  | 'footnote'

/** 正文里的行内公式：start/end 是段落文本中的位置。 */
export interface InlineMath {
  start: number
  end: number
  latex: string
  /** 规则无法可靠重建、由解析器裁了小图兜底 */
  fallback: boolean
  source: string
  image_path?: string | null
}

export interface Block {
  id: string
  order: number
  page: number
  kind: BlockKind
  level: number
  section: string
  text: string
  translated: string
  image_path: string | null
  table_html: string
  /** 公式的 LaTeX（识别成功后由后端写入；为空时前端回落到 image_path） */
  latex?: string
  /** 公式编号（PDF 里印出来的 (3) / (2.1)） */
  eq_number?: string
  /** 正文里的行内公式（解析阶段按字体/字号/基线重建） */
  inline_math?: InlineMath[]
  caption: string
  translatable: boolean
  bbox: number[]
  /** 跨栏/跨页块的逐页矩形 [page, x0, y0, x1, y1]；单页块不返回 */
  bboxes?: number[][]
}

/** 阅读页划词后点「提问」，作为引用带入左侧问答的片段。 */
export interface ReaderQuote {
  /** 去重键：同一块（或同一页）的同一段文字只保留一条 */
  id: string
  /** 选中的文字（原文 / 译文 / 原版 PDF 文本层） */
  text: string
  /** 1 起的页码 */
  page?: number
  /** 重排视图里的块 id；原版 PDF 没有块 */
  blockId?: string
  source: 'en' | 'zh' | 'pdf'
}

/** 划词提问时上报的引用草稿：id 由阅读页统一生成，用于去重与删除。 */
export type ReaderQuoteDraft = Omit<ReaderQuote, 'id'>

/** 点击图表/公式旁的小圆圈后，加入左侧问答「附录」的条目。 */
export interface ReaderContextItem {
  /** 与 blockId 相同，用于去重 */
  id: string
  blockId: string
  kind: 'equation' | 'figure' | 'table'
  label: string
  page: number
  caption?: string
  latex?: string
  imageUrl?: string
}

export interface Paper {
  paper_id: string
  title: string
  /** 标题译文（有则中文作主标题、英文作副标题） */
  title_zh?: string
  authors: string
  affiliation: string
  page_count: number
  /** 每页 [宽, 高]（PDF 点），原版 PDF 视图用它给页图占位，跳转才不会错位 */
  page_sizes?: [number, number][]
  columns: number
  glossary: Record<string, string>
  /** 译文目标语言代码（zh-Hans / ja / fr…）；空表示中文 */
  translation_lang?: string
  /** 上次阅读位置，用于打开时回到原处 */
  progress?: { pct: number; block_id: string; page: number }
  blocks: Block[]
}

/** 阅读模式：对照（中上英下）/ 纯中文 / 纯英文 */
export type ViewMode = 'bilingual' | 'zh' | 'en'
