import { useMemo, type CSSProperties, type RefObject } from 'react'
import type { Paper, ReaderContextItem, ViewMode } from '../types'
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
  onOpenSettings?: () => void
  annotations?: Annotation[]
  annotationActions?: AnnotationActions
  highlights?: ReadingHighlight[]
  /** 已加入问答附录的块 */
  contextIds?: Set<string>
  onAttachContext?: (item: ReaderContextItem) => void
  /** 双击正文里的高亮/批注：切到左侧批注栏并定位 */
  onOpenAnnotation?: (annotation: Annotation) => void
}

export function Reader({
  paper,
  mode,
  scale,
  scrollRef,
  hint,
  canTranslate = true,
  onOpenSettings,
  annotations = [],
  annotationActions,
  highlights = [],
  contextIds,
  onAttachContext,
  onOpenAnnotation,
}: Props) {
  const prepared = useMemo(() => prepareBlocks(paper.blocks), [paper.blocks])
  const flow = prepared.filter((r) => isFlowBlock(r.block))
  const refs = paper.blocks.filter((b) => b.kind === 'reference')
  const paragraphs = paper.blocks.filter((b) => b.kind === 'para').length
  const translated = paper.blocks.filter((b) => b.translated).length
  const hasTranslation = translated > 0
  const terms = paper.glossary ?? {}
  const effectiveMode: ViewMode = hasTranslation ? mode : 'en'

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
            <summary>Author</summary>
            {paper.authors && <p className="hero-authors">{paper.authors}</p>}
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

        {(!hasTranslation || !canTranslate) && hint && (
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
            hasTranslation={hasTranslation && canTranslate}
            paperId={paper.paper_id}
            annotations={annotations}
            annotationActions={annotationActions}
            highlights={highlights}
            attachedIds={contextIds}
            onAttachContext={onAttachContext}
            onOpenAnnotation={onOpenAnnotation}
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
