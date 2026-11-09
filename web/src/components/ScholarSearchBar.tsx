import { IconClose, IconSearch } from './icons'
import { ScholarFilters } from './ScholarFilters'
import { SOURCE_META, type ScholarSource, type SearchParams } from '../lib/scholar'

interface Props {
  input: string
  onInput: (value: string) => void
  onSubmit: () => void
  onClear: () => void
  params: SearchParams
  onChange: (patch: Partial<SearchParams>) => void
  advanced: boolean
  onToggleAdvanced: () => void
  loading: boolean
  translatedQuery: string | null
  disabledSources: Partial<Record<ScholarSource, string>>
}

/** 检索页顶部：数据源切换、搜索框、高级检索。 */
export function ScholarSearchBar({
  input,
  onInput,
  onSubmit,
  onClear,
  params,
  onChange,
  advanced,
  onToggleAdvanced,
  loading,
  translatedQuery,
  disabledSources,
}: Props) {
  const placeholder =
    params.source === 'pubmed' || params.source === 'europepmc'
      ? '例如：CRISPR cancer therapy'
      : '例如：large language model reasoning'

  return (
    <>
      <header className="library-head scholar-head">
        <div>
          <h1>文献检索</h1>
        </div>
        <button
          type="button"
          className="btn ghost"
          aria-expanded={advanced}
          onClick={onToggleAdvanced}
        >
          {advanced ? '收起高级检索' : '高级检索'}
        </button>
      </header>

      <div className="scholar-sources" role="tablist" aria-label="数据源">
        {SOURCE_META.map((s) => {
          const disabledReason = disabledSources[s.k]
          const tab = (
            <button
              role="tab"
              aria-selected={params.source === s.k}
              data-active={params.source === s.k}
              disabled={!!disabledReason}
              aria-label={disabledReason || s.l}
              title={disabledReason ? undefined : s.d}
              onClick={() =>
                onChange({
                  source: s.k,
                  field: 'all',
                  phrase: false,
                  category: '',
                  fromYear: null,
                  toYear: null,
                })
              }
            >
              {s.l}
            </button>
          )
          // 禁用按钮在部分浏览器里不触发 hover，用外层 span 承载气泡提示
          return disabledReason ? (
            <span
              key={s.k}
              className="scholar-source-off"
              data-tip={disabledReason}
              title={disabledReason}
            >
              {tab}
            </span>
          ) : (
            <span key={s.k} className="scholar-source">
              {tab}
            </span>
          )
        })}
      </div>

      <form
        className="scholar-search"
        onSubmit={(e) => {
          e.preventDefault()
          onSubmit()
        }}
      >
        <IconSearch />
        <input
          value={input}
          data-tour="search-input"
          onChange={(e) => onInput(e.target.value)}
          placeholder={placeholder}
          aria-label="检索词"
          spellCheck={false}
        />
        {input && (
          <button type="button" className="scholar-clear" onClick={onClear} title="清空">
            <IconClose />
          </button>
        )}
        <button className="btn primary" type="submit" disabled={!input.trim() || loading}>
          {loading ? '检索中…' : '检索'}
        </button>
      </form>

      {translatedQuery && (
        <p className="scholar-hint">
          中文查询已译为英文检索：<b lang="en">{translatedQuery}</b>
        </p>
      )}

      {advanced && <ScholarFilters params={params} onChange={onChange} />}
    </>
  )
}
