import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  researchApi,
  type ReviewTemplateGuide,
  type ReviewTemplateOption,
  type ReviewTemplateOutlineItem,
  type ReviewTemplateState,
  type ReviewTemplateStyle,
} from '../lib/researchApi'
import { IconCheck, IconPlus, IconSearch, IconTrash, IconUpload } from './icons'

const SOURCE_LABEL: Record<string, string> = {
  default: '系统默认',
  builtin: '内置模板',
  upload: '我上传的',
  paper: '文献库综述',
}

type Preview = ReviewTemplateOption & { error: string }

const HEADING_LABEL: Record<string, string> = {
  roman: '罗马数字（I. II.）',
  arabic: '阿拉伯数字（1. 2.）',
  letter: '大写字母（A. B.）',
  chinese: '中文编号（一、二、）',
  none: '不编号',
  unknown: '未识别',
}

const CITATION_LABEL: Record<string, string> = {
  numeric: '数字编号',
  'author-year': '作者-年份',
  mixed: '混合',
  unknown: '未识别',
}

function languageRows(style?: ReviewTemplateStyle): Array<[string, string]> {
  if (!style) return []
  return [
    ['引用方式', CITATION_LABEL[style.citationStyle] || style.citationStyle || '—'],
    ['平均句长', `${style.avgSentenceLength || 0} ${style.sentenceUnit || '词'}`],
    ['长句占比', `${Math.round((style.longSentenceRatio || 0) * 100)}%`],
    [
      '常用衔接',
      (style.connectives ?? []).map((row) => row.term).join('、') || '—',
    ],
    ['限定表达', (style.hedges ?? []).map((row) => row.term).join('、') || '—'],
  ]
}

function formatRows(guide: ReviewTemplateGuide): Array<[string, string]> {
  const fmt = guide.format || {}
  const margins = fmt.marginsMm
    ? (['left', 'right', 'top', 'bottom'] as const)
        .filter((key) => fmt.marginsMm?.[key] !== undefined)
        .map((key) => `${key} ${fmt.marginsMm?.[key]}mm`)
        .join('、')
    : ''
  const caption =
    fmt.captionSeparator === ':'
      ? '编号后用冒号（Figure 1: …）'
      : fmt.captionSeparator === '.'
        ? '编号后用句点（Figure 1. …）'
        : fmt.captionSeparator === ''
          ? '编号后不加分隔符'
          : '未识别'
  return [
    [
      '页面',
      [
        fmt.pageSize,
        fmt.columns === 2 ? '双栏' : fmt.columns === 1 ? '单栏' : '',
        fmt.bodyFontSize ? `正文 ${fmt.bodyFontSize}pt` : '',
      ]
        .filter(Boolean)
        .join(' · ') || '—',
    ],
    ['页边距', margins || '—'],
    [
      '标题编号',
      (HEADING_LABEL[fmt.headingStyle ?? ''] || '未识别') +
        (fmt.headingExample ? `（例：${fmt.headingExample}）` : ''),
    ],
    ['图表题', caption],
    [
      '摘要 / 关键词',
      `${fmt.abstract ? '有' : '无'} / ${fmt.keywords ? '有' : '无'}`,
    ],
    [
      '参考文献',
      (fmt.referenceHeading || '未识别') +
        (fmt.citationStyle ? `（${CITATION_LABEL[fmt.citationStyle] || fmt.citationStyle}）` : ''),
    ],
  ]
}

function TemplateOutlineView({
  outline,
}: {
  outline: ReviewTemplateOutlineItem[]
}) {
  return (
    <div className="template-outline">
      <div className="template-outline-head">
        <span>章节约束</span>
        <em>{outline.length} 节 · 点击章节展开</em>
      </div>
      {outline.map((item, index) => (
        <details
          className="template-outline-item"
          key={item.title}
          open={index === 0}
        >
          <summary>
            <b>{item.title}</b>
            {item.words && <span>{item.words}</span>}
          </summary>
          <div className="template-outline-body">
            {item.points.length > 0 && (
              <div className="template-outline-block">
                <h5>必写</h5>
                <ul>
                  {item.points.map((point) => (
                    <li key={point}>{point}</li>
                  ))}
                </ul>
              </div>
            )}
            {item.checks.length > 0 && (
              <div className="template-outline-block is-checks">
                <h5>自检</h5>
                <ul>
                  {item.checks.map((check) => (
                    <li key={check}>{check}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </details>
      ))}
    </div>
  )
}

function TemplateGuideView({
  guide,
  defaultOpen = false,
}: {
  guide: ReviewTemplateGuide
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const sections = guide.sections ?? []
  const figures = guide.figures ?? []
  const tables = guide.tables ?? []
  const items = [...figures, ...tables]
  const stats = (guide.sectionStats ?? []).filter((row) => row.figures || row.tables)
  const images = (guide.sectionStats ?? []).reduce(
    (total, row) => total + (row.images ?? 0),
    0,
  )
  const fmt = guide.format
  const style = guide.style
  const citation = CITATION_LABEL[style?.citationStyle ?? ''] || '—'
  const tiles = [
    { label: '章节', value: sections.length ? `${sections.length} 章` : '—' },
    {
      label: '图表',
      value:
        figures.length || tables.length
          ? `${figures.length} 图 · ${tables.length} 表`
          : images
            ? `${images} 张位图`
            : '—',
    },
    {
      label: '格式',
      value: fmt
        ? [fmt.pageSize, fmt.columns === 2 ? '双栏' : '单栏']
            .filter(Boolean)
            .join(' · ') || '—'
        : '—',
    },
    {
      label: '语言',
      value: style
        ? `${citation} · ${style.avgSentenceLength || 0} ${style.sentenceUnit || '词'}`
        : '—',
    },
  ]

  return (
    <div className="template-guide">
      <div className="template-guide-head">
        <span className="template-guide-label">模板解析结果</span>
        <button
          type="button"
          className="link-btn"
          onClick={() => setOpen((value) => !value)}
        >
          {open ? '收起详情' : '展开详情'}
        </button>
      </div>
      <div className="template-guide-grid">
        {tiles.map((tile) => (
          <div className="template-guide-tile" key={tile.label}>
            <span>{tile.label}</span>
            <b>{tile.value}</b>
          </div>
        ))}
      </div>
      {!open && (stats.length > 0 || items.length > 0) && (
        <p className="template-hint">
          {stats.length > 0
            ? '图表分布：' +
              stats
                .slice(0, 3)
                .map((row) => `${row.title}（${row.figures} 图 ${row.tables} 表）`)
                .join('、')
            : ''}
          {items.length > 0
            ? (stats.length > 0 ? '；' : '') +
              `${items[0].label} · ${items[0].type ?? '图表'}：${items[0].purpose ?? ''}`
            : ''}
        </p>
      )}
      {open && (
        <div className="template-guide-detail">
          {sections.length > 0 && (
            <>
              <h4>章节骨架</h4>
              <div className="template-chips">
                {sections.map((section) => (
                  <span key={section}>{section}</span>
                ))}
              </div>
            </>
          )}
          {items.length > 0 && (
            <>
              <h4>图表放置与制作依据</h4>
              <ul className="template-guide-list">
                {items.slice(0, 10).map((item) => (
                  <li key={item.label}>
                    <b>{item.label}</b>
                    {item.type ? ` · ${item.type}` : ''}{' '}
                    <span>
                      {item.section || '未识别'}
                      {item.page ? ` p${item.page}` : ''}
                    </span>
                    <p>
                      {item.purpose}
                      {item.context ? `；正文引用：“${item.context}”` : ''}
                    </p>
                  </li>
                ))}
              </ul>
            </>
          )}
          {fmt && (
            <>
              <h4>论文格式</h4>
              <dl className="template-format">
                {formatRows(guide).map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
          {style && (
            <>
              <h4>语言风格</h4>
              <dl className="template-format">
                <div>
                  <dt>引用方式</dt>
                  <dd>{citation}</dd>
                </div>
                <div>
                  <dt>平均句长</dt>
                  <dd>
                    {style.avgSentenceLength} {style.sentenceUnit}
                  </dd>
                </div>
                <div>
                  <dt>长句占比</dt>
                  <dd>{Math.round((style.longSentenceRatio || 0) * 100)}%</dd>
                </div>
                <div>
                  <dt>常用衔接</dt>
                  <dd>
                    {(style.connectives ?? []).map((row) => row.term).join('、') || '—'}
                  </dd>
                </div>
                <div>
                  <dt>限定表达</dt>
                  <dd>{(style.hedges ?? []).map((row) => row.term).join('、') || '—'}</dd>
                </div>
              </dl>
            </>
          )}
          {(guide.notes?.length ?? 0) > 0 && (
            <>
              <h4>写作提示</h4>
              <ul className="template-guide-list is-notes">
                {guide.notes?.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  )
}

export function ReviewTemplatePanel() {
  const [state, setState] = useState<ReviewTemplateState | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [previewing, setPreviewing] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const apply = useCallback((next: ReviewTemplateState, message = '') => {
    setState(next)
    setError('')
    if (message) setNotice(message)
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      apply(await researchApi.getReviewTemplate())
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取模板失败')
    } finally {
      setLoading(false)
    }
  }, [apply])

  useEffect(() => {
    void load()
  }, [load])

  const choose = async (id: string) => {
    if (!state || saving) return
    if (state.selected === id) {
      setNotice('已经在使用这个模板')
      return
    }
    setSaving(true)
    setNotice('')
    try {
      apply(await researchApi.selectReviewTemplate(id), '已保存，之后写综述会按这个骨架检查')
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const upload = async (file: File | undefined) => {
    if (!file) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      apply(await researchApi.uploadReviewTemplate(file), `已上传「${file.name}」并设为当前模板`)
    } catch (e) {
      setError(e instanceof Error ? e.message : '上传失败')
    } finally {
      setSaving(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const remove = async (option: ReviewTemplateOption) => {
    if (!window.confirm(`删除模板「${option.name}」？`)) return
    setSaving(true)
    setError('')
    try {
      apply(
        await researchApi.deleteReviewTemplateUpload(
          option.id.replace(/^upload:/, ''),
        ),
        '已删除模板',
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除失败')
    } finally {
      setSaving(false)
    }
  }

  const previewPaper = async (paperId: string) => {
    setPreviewing(paperId)
    setError('')
    try {
      setPreview(await researchApi.previewReviewTemplatePaper(paperId))
    } catch (e) {
      setError(e instanceof Error ? e.message : '解析这篇论文失败')
    } finally {
      setPreviewing('')
    }
  }

  const refreshSelected = async () => {
    const id = state?.selectedDetail?.id ?? ''
    if (!id.startsWith('paper:')) return
    setRefreshing(true)
    setError('')
    setNotice('')
    try {
      await researchApi.previewReviewTemplatePaper(id.slice(6), true)
      apply(await researchApi.getReviewTemplate(), '已重新解析模板的图表与格式')
    } catch (e) {
      setError(e instanceof Error ? e.message : '重新解析失败')
    } finally {
      setRefreshing(false)
    }
  }

  const refreshPreview = async () => {
    if (!preview) return
    setRefreshing(true)
    setError('')
    setNotice('')
    try {
      setPreview(
        await researchApi.previewReviewTemplatePaper(
          preview.id.replace(/^paper:/, ''),
          true,
        ),
      )
      setNotice('已重新解析这篇综述的图表与格式')
    } catch (e) {
      setError(e instanceof Error ? e.message : '重新解析失败')
    } finally {
      setRefreshing(false)
    }
  }

  const papers = useMemo(() => {
    const rows = state?.library ?? []
    const needle = query.trim().toLowerCase()
    const filtered = needle
      ? rows.filter(
          (row) =>
            row.title.toLowerCase().includes(needle) ||
            row.authors.toLowerCase().includes(needle),
        )
      : rows
    return [...filtered].sort(
      (a, b) => Number(b.isReview) - Number(a.isReview),
    )
  }, [state, query])

  if (loading) {
    return (
      <div className="modal-body template-panel">
        <p className="model-empty">正在读取模板设置…</p>
      </div>
    )
  }

  const detail = state?.selectedDetail
  const activeSource = detail?.source || 'default'
  const activeKind = detail?.kind || (activeSource === 'default' ? 'review' : '')
  const uploads = (state?.templates ?? []).filter((item) => item.source === 'upload')
  const builtins = (state?.templates ?? []).filter(
    (item) => item.source === 'default' || item.source === 'builtin',
  )
  const paperTemplates = builtins.filter((item) => item.kind === 'paper')
  const reviewTemplates = builtins.filter((item) => item.kind !== 'paper')

  const renderTemplateCard = (item: ReviewTemplateOption) => (
    <button
      key={item.id}
      type="button"
      className="template-card"
      data-active={state?.selected === item.id}
      disabled={saving}
      onClick={() => void choose(item.id)}
    >
      <div className="template-card-top">
        <b>{item.name}</b>
        <span className="template-card-flags">
          <span className="template-card-kind" data-kind={item.kind || 'review'}>
            {item.kind === 'paper' ? '论文' : '综述'}
          </span>
          {state?.selected === item.id && <IconCheck />}
        </span>
      </div>
      <p>{item.description}</p>
      {item.sections.length > 0 && (
        <div className="template-card-sections">
          {item.sections.slice(0, 3).map((section) => (
            <span key={section}>{section}</span>
          ))}
          {item.sections.length > 3 && (
            <em>+{item.sections.length - 3}</em>
          )}
        </div>
      )}
    </button>
  )

  const previewGuide = preview?.guide
  const previewItems = [
    ...(previewGuide?.figures ?? []),
    ...(previewGuide?.tables ?? []),
  ]
  const previewNotes = previewGuide?.notes ?? []
  const previewLang = languageRows(previewGuide?.style)

  return (
    <div className="modal-body template-panel">
      {error && <div className="template-alert">{error}</div>}
      {notice && !error && (
        <div className="template-alert is-ok">
          <IconCheck /> {notice}
        </div>
      )}

      {detail && (
        <section className="template-current">
          <div className="template-current-main">
            <span className="template-eyebrow">
              {activeKind === 'paper'
                ? '当前论文模板'
                : activeKind === 'review'
                  ? '当前综述模板'
                  : '当前写作模板'}
            </span>
            <b>{detail.name || '通用综述（五段式）'}</b>
            <span className="template-source">
              {SOURCE_LABEL[activeSource] || activeSource}
              {detail.sections.length ? ` · ${detail.sections.length} 个章节` : ''}
            </span>
          </div>
          <span className="template-badge" data-source={activeSource}>
            {activeSource === 'default' ? '默认' : SOURCE_LABEL[activeSource]}
          </span>
          {detail.sections.length > 0 && (
            <div className="template-chips">
              {detail.sections.map((section) => (
                <span key={section}>{section}</span>
              ))}
            </div>
          )}
          {detail.outline && detail.outline.length > 0 ? (
            <TemplateOutlineView outline={detail.outline} />
          ) : detail.sections.length === 0 ? (
            <p className="template-hint">
              这个模板还没有解析出章节骨架。写作时智能体会先读取原文再补出骨架。
            </p>
          ) : null}
          {detail.guide && Object.keys(detail.guide).length > 0 && (
            <TemplateGuideView guide={detail.guide} />
          )}
          {detail.source === 'paper' && (
            <button
              type="button"
              className="link-btn template-refresh"
              disabled={refreshing || saving}
              onClick={() => void refreshSelected()}
            >
              {refreshing ? '重新解析中…' : '重新解析模板'}
            </button>
          )}
          {detail.error && (
            <p className="template-hint is-warn">
              {detail.error}。写作时智能体会先读取这篇文献再补出章节骨架。
            </p>
          )}
        </section>
      )}

      <section className="model-section">
        <div className="section-head">
          <h3>论文模板</h3>
          <span>有数据与实验的原创论文 · 点击卡片切换</span>
        </div>
        {paperTemplates.length > 0 ? (
          <div className="preset-grid">
            {paperTemplates.map(renderTemplateCard)}
          </div>
        ) : (
          <p className="model-empty">当前版本没有内置论文模板。</p>
        )}
      </section>

      <section className="model-section">
        <div className="section-head">
          <h3>综述模板</h3>
          <span>系统综述 / 叙述性综述 / 通用五段式</span>
        </div>
        <div className="preset-grid">
          {reviewTemplates.map(renderTemplateCard)}
        </div>
      </section>

      <section className="model-section">
        <div className="section-head">
          <h3>我上传的模板</h3>
          <button
            className="btn small"
            type="button"
            disabled={saving}
            onClick={() => fileRef.current?.click()}
          >
            <IconUpload /> 上传 Markdown 模板
          </button>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept=".md,.markdown,.txt,text/markdown,text/plain"
          hidden
          onChange={(e) => void upload(e.target.files?.[0])}
        />
        {uploads.length === 0 ? (
          <p className="model-empty">
            还没有上传模板。支持 .md / .txt，用二级标题（## 章节名）描述骨架，
            上传后会立即设为当前模板。
          </p>
        ) : (
          <div className="template-upload-list">
            {uploads.map((item) => (
              <div
                className="template-upload-row"
                key={item.id}
                data-active={state?.selected === item.id}
              >
                <button
                  type="button"
                  className="template-upload-pick"
                  onClick={() => void choose(item.id)}
                  disabled={saving}
                >
                  <b>{item.name}</b>
                  <span>{item.description}</span>
                </button>
                {item.sections.length > 0 && (
                  <div className="template-card-sections">
                    {item.sections.slice(0, 4).map((section) => (
                      <span key={section}>{section}</span>
                    ))}
                    {item.sections.length > 4 && (
                      <em>+{item.sections.length - 4}</em>
                    )}
                  </div>
                )}
                <button
                  type="button"
                  className="icon-btn danger"
                  title="删除模板"
                  disabled={saving}
                  onClick={() => void remove(item)}
                >
                  <IconTrash />
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="model-section">
        <div className="section-head">
          <h3>从文献库选择综述当模板</h3>
          <span>按标题或作者搜索；首次预览会解析 PDF 目录</span>
        </div>
        <label className="template-search">
          <IconSearch />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索文献库里的综述，如 survey / review / 综述"
            spellCheck={false}
          />
        </label>
        <div className="template-paper-list">
          {papers.length === 0 ? (
            <p className="model-empty">没有匹配的文献。</p>
          ) : (
            papers.map((paper) => (
              <div className="template-paper" key={paper.id}>
                <div className="template-paper-main">
                  <b>{paper.title}</b>
                  <span>
                    {paper.authors || '作者未知'}
                    {paper.year ? ` · ${paper.year}` : ''}
                    {paper.pages ? ` · ${paper.pages} 页` : ''}
                  </span>
                </div>
                {paper.isReview && <span className="template-tag">综述</span>}
                <button
                  type="button"
                  className="btn small"
                  disabled={previewing === paper.id}
                  onClick={() => void previewPaper(paper.id)}
                >
                  {previewing === paper.id ? '解析中…' : '预览'}
                </button>
              </div>
            ))
          )}
        </div>

        {preview && (
          <div className="template-preview is-wide">
            <div className="template-preview-head">
              <div>
                <b>{preview.name}</b>
                <span>
                  {preview.error
                    ? '没能自动提取章节'
                    : `提取到 ${preview.sections.length} 个章节 · ${previewItems.length} 个图表`}
                </span>
              </div>
              <div className="template-preview-actions">
                <button
                  type="button"
                  className="link-btn"
                  disabled={refreshing}
                  onClick={() => void refreshPreview()}
                >
                  {refreshing ? '重新解析中…' : '重新解析'}
                </button>
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => setPreview(null)}
                >
                  收起
                </button>
              </div>
            </div>
            <div className="template-preview-grid">
              <section className="template-preview-col">
                <h4>章节骨架</h4>
                <span className="template-preview-meta">
                  {preview.sections.length} 章
                </span>
                {preview.error ? (
                  <p className="template-hint is-warn">{preview.error}</p>
                ) : (
                  <ol className="template-section-list">
                    {preview.sections.map((section) => (
                      <li key={section}>{section}</li>
                    ))}
                  </ol>
                )}
              </section>
              <section className="template-preview-col">
                <h4>图表放置与制作依据</h4>
                <span className="template-preview-meta">
                  {previewGuide?.figures?.length ?? 0} 图 ·{' '}
                  {previewGuide?.tables?.length ?? 0} 表
                </span>
                {previewItems.length > 0 ? (
                  <ul className="template-guide-list">
                    {previewItems.slice(0, 12).map((item) => (
                      <li key={item.label}>
                        <b>{item.label}</b>
                        {item.type ? ` · ${item.type}` : ''}{' '}
                        <span>
                          {item.section || '未识别'}
                          {item.page ? ` p${item.page}` : ''}
                        </span>
                        <p>
                          {item.purpose}
                          {item.context ? `；正文引用：“${item.context}”` : ''}
                        </p>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="template-hint">
                    文本层没有取到图题。写作时可按各章页数与密度安排示意图或表，不要虚构具体图题。
                  </p>
                )}
              </section>
              <section className="template-preview-col">
                <h4>论文格式</h4>
                {previewGuide?.format ? (
                  <dl className="template-format is-single">
                    {formatRows(previewGuide).map(([label, value]) => (
                      <div key={label}>
                        <dt>{label}</dt>
                        <dd>{value}</dd>
                      </div>
                    ))}
                  </dl>
                ) : (
                  <p className="template-hint">未解析到格式信息。</p>
                )}
                {previewLang.length > 0 && (
                  <>
                    <h4>语言风格</h4>
                    <dl className="template-format is-single">
                      {previewLang.map(([label, value]) => (
                        <div key={label}>
                          <dt>{label}</dt>
                          <dd>{value}</dd>
                        </div>
                      ))}
                    </dl>
                  </>
                )}
                {previewNotes.length > 0 && (
                  <>
                    <h4>写作提示</h4>
                    <ul className="template-guide-list is-notes">
                      {previewNotes.slice(0, 5).map((note) => (
                        <li key={note}>{note}</li>
                      ))}
                    </ul>
                  </>
                )}
              </section>
            </div>
            <div className="template-preview-foot">
              <span>图表、格式与语言为自动解析结果，写作时可按实际证据调整。</span>
              <button
                type="button"
                className="btn primary small"
                disabled={saving}
                onClick={() => void choose(preview.id)}
              >
                <IconPlus /> 使用这篇综述的骨架
              </button>
            </div>
          </div>
        )}
      </section>

      <p className="template-footer">
        优先级：本次对话指定 &gt; 单个项目设置 &gt; 这里选的全局模板 &gt; 系统默认。
        文献库模板会影响章节骨架、图表安排与语言风格，不会改动已写好的正文。
      </p>
    </div>
  )
}
