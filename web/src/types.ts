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
  caption: string
  translatable: boolean
  bbox: number[]
}

export interface Paper {
  paper_id: string
  title: string
  /** 标题译文（有则中文作主标题、英文作副标题） */
  title_zh?: string
  authors: string
  affiliation: string
  page_count: number
  columns: number
  glossary: Record<string, string>
  blocks: Block[]
}

/** 阅读模式：对照（中上英下）/ 纯中文 / 纯英文 */
export type ViewMode = 'bilingual' | 'zh' | 'en'
