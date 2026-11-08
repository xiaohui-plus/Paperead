import { useEffect, useState } from 'react'
import { PER_PAGE, FIELD_META, type SearchParams } from '../lib/scholar'

interface Props {
  params: SearchParams
  onChange: (patch: Partial<SearchParams>) => void
}

function CommitInput({
  value,
  onCommit,
  placeholder,
  inputMode,
}: {
  value: string
  onCommit: (next: string) => void
  placeholder: string
  inputMode?: 'numeric'
}) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])
  const commit = () => {
    if (draft.trim() !== value) onCommit(draft.trim())
  }
  return (
    <input
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          commit()
        }
      }}
      placeholder={placeholder}
      inputMode={inputMode}
    />
  )
}

/** 高级检索：字段、分类、年份区间、每页条数、短语精确匹配。 */
export function ScholarFilters({ params, onChange }: Props) {
  const isArxiv = params.source === 'arxiv'
  const hasFilters =
    params.field !== 'all' ||
    params.phrase ||
    !!params.category ||
    params.fromYear != null ||
    params.toYear != null

  return (
    <div className="scholar-filters">
      <label>
        <span>检索字段</span>
        <select
          value={params.field}
          onChange={(e) => onChange({ field: e.target.value as SearchParams['field'] })}
        >
          {FIELD_META.map((f) => (
            <option key={f.k} value={f.k}>
              {f.l}
            </option>
          ))}
        </select>
      </label>

      {isArxiv && (
        <label>
          <span>分类（cat）</span>
          <CommitInput
            value={params.category}
            onCommit={(value) => onChange({ category: value })}
            placeholder="如 cs.CL、math.AG"
          />
        </label>
      )}

      <label>
        <span>起始年份</span>
        <CommitInput
          value={params.fromYear ? String(params.fromYear) : ''}
          onCommit={(value) => onChange({ fromYear: /^\d{4}$/.test(value) ? Number(value) : null })}
          placeholder="1991"
          inputMode="numeric"
        />
      </label>

      <label>
        <span>截止年份</span>
        <CommitInput
          value={params.toYear ? String(params.toYear) : ''}
          onCommit={(value) => onChange({ toYear: /^\d{4}$/.test(value) ? Number(value) : null })}
          placeholder={String(new Date().getFullYear())}
          inputMode="numeric"
        />
      </label>

      <label>
        <span>每页条数</span>
        <select
          value={params.perPage}
          onChange={(e) => onChange({ perPage: Number(e.target.value) })}
        >
          {PER_PAGE.map((n) => (
            <option key={n} value={n}>
              {n} 条
            </option>
          ))}
        </select>
      </label>

      <label className="scholar-check">
        <input
          type="checkbox"
          checked={params.phrase}
          onChange={(e) => onChange({ phrase: e.target.checked })}
        />
        短语精确匹配
      </label>

      {hasFilters && (
        <button
          type="button"
          className="btn ghost scholar-reset"
          onClick={() =>
            onChange({ field: 'all', phrase: false, category: '', fromYear: null, toYear: null })
          }
        >
          清除筛选
        </button>
      )}
    </div>
  )
}
