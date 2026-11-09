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
  caption: string
  translatable: boolean
  bbox: number[]
}

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
