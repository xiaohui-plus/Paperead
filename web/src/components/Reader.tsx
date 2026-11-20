import { useMemo, type CSSProperties, type RefObject } from 'react'
import type { Paper, ReaderContextItem, ReaderQuoteDraft, ViewMode } from '../types'
import { isFlowBlock, prepareBlocks } from '../lib/blocks'
import type { Annotation, ReadingHighlight } from '../lib/library'
import type { AnnotationActions } from './Annotations'
import { BlockView } from './BlockView'

interface Props {
  paper: Paper
  mode: ViewMode
  scale: number
  scrollRef: RefObject<HTMLDivElement | null>
  /** 没有译文时顶部提示的文案 */
  hint?: string
  /** 是否配置了回答模型：没配置时不要显示"等待翻译"的骨架条 */
  canTranslate?: boolean
  translationFailures?: Record<string, number>
  onRetryTranslation?: (id: string) => void
  /** 是否开启自动翻译：关闭后没翻的段落不应继续显示骨架条 */
  autoTranslate?: boolean
  onOpenSettings?: () => void
  annotations?: Annotation[]
  annotationActions?: AnnotationActions
  highlights?: ReadingHighlight[]
  /** 已加入问答附录的块 */
  contextIds?: Set<string>
  onAttachContext?: (item: ReaderContextItem) => void
  /** 划词工具条点「提问」：把选中的文字带到左侧问答面板 */
  onAskQuote?: (quote: ReaderQuoteDraft) => void
  /** 双击正文里的高亮/批注：切到左侧批注栏并定位 */
  onOpenAnnotation?: (annotation: Annotation) => void
  /** 双击自动阅读重点：切到左侧「阅读重点」并定位 */
  onOpenHighlight?: (highlight: ReadingHighlight) => void
}

export function Reader({
  paper,
  mode,
  scale,
  scrollRef,
  hint,
  canTranslate = true,
  translationFailures = {},
  onRetryTranslation,
  autoTranslate = true,
  onOpenSettings,
  annotations = [],
  annotationActions,
  highlights = [],
  contextIds,
  onAttachContext,
  onAskQuote,
  onOpenAnnotation,
  onOpenHighlight,
}: Props) {
  const prepared = useMemo(() => prepareBlocks(paper.blocks), [paper.blocks])
  const flow = prepared.filter((r) => isFlowBlock(r.block))
  const refs = paper.blocks.filter((b) => b.kind === 'reference')
  const paragraphs = paper.blocks.filter((b) => b.kind === 'para').length
  const translated = paper.blocks.filter((b) => b.translated).length
  const hasTranslation = translated > 0
  const terms = paper.glossary ?? {}
  const effectiveMode: ViewMode = hasTranslation ? mode : 'en'
  // 本地 PDF 可能没有可用的元数据作者；回退到解析出的 author 块，
  // 保证封面开头信息始终有归处，而不是散落在正文流里。
  const blockAuthors = paper.blocks
    .filter((b) => b.kind === 'author')
    .map((b) => b.text.trim())
    .filter(Boolean)
    .join(' ')
  const authors = paper.authors || blockAuthors

  return (
    <div className="main-scroll" ref={scrollRef}>
      <article
        className={`reader mode-${effectiveMode}`}
        style={{ '--read-scale': String(scale) } as CSSProperties}
      >
        <header className="hero">
          <h1>{paper.title}</h1>
          {paper.title_zh && <p className="hero-title-zh">{paper.title_zh}</p>}

          <details className="hero-author">
            <summary>作者与单位</summary>
            {authors && <p className="hero-authors">{authors}</p>}
            {paper.affiliation && <p className="hero-affil">{paper.affiliation}</p>}
          </details>

          <div className="hero-meta">
            <span>
              <b>{paper.page_count}</b> 页
            </span>
            <span>
              <b>{paper.columns}</b> 栏
            </span>
            <span>
              <b>{paragraphs}</b> 正文段落
            </span>
            <span>
              <b>{translated}</b> 已翻译
            </span>
            <span>
              <b>{Object.keys(terms).length}</b> 术语
            </span>
          </div>
        </header>

        {(!hasTranslation || !canTranslate || !autoTranslate) && hint && (
          <div className="reader-banner">
            <span>{hint}</span>
            {!canTranslate && onOpenSettings && (
              <button className="banner-action" onClick={onOpenSettings}>
                去设置
              </button>
            )}
          </div>
        )}

        {flow.map((rb) => (
          <BlockView
            key={rb.block.id}
            rb={rb}
            mode={effectiveMode}
            terms={terms}
            hasTranslation={hasTranslation && canTranslate && autoTranslate}
            translationFailed={autoTranslate && canTranslate && (translationFailures[rb.block.id] ?? 0) >= 2}
            onRetryTranslation={onRetryTranslation}
            paperId={paper.paper_id}
            annotations={annotations}
            annotationActions={annotationActions}
            highlights={highlights}
            attachedIds={contextIds}
            onAttachContext={onAttachContext}
            onAskQuote={onAskQuote}
            onOpenAnnotation={onOpenAnnotation}
            onOpenHighlight={onOpenHighlight}
          />
        ))}

        {refs.length > 0 && (
          <details className="refs">
            <summary>参考文献 · {refs.length} 条（不翻译）</summary>
            {refs.map((r, i) => (
              <p className="ref-item" key={r.id}>
                <b>{i + 1}</b>
                {r.text}
              </p>
            ))}
          </details>
        )}
      </article>
    </div>
  )
}
