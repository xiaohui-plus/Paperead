import { useEffect, useMemo, useState } from 'react'

const MAX_ROWS = 500
const MAX_COLS = 60

export interface Sheet {
  name: string
  rows: string[][]
}

/** 解析 CSV / TSV，支持引号包裹的逗号、换行与转义引号。 */
export function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false
  const source = text.replace(/^\uFEFF/, '')

  for (let index = 0; index < source.length; index += 1) {
    const char = source[index]
    if (quoted) {
      if (char === '"') {
        if (source[index + 1] === '"') {
          cell += '"'
          index += 1
        } else {
          quoted = false
        }
      } else {
        cell += char
      }
      continue
    }
    if (char === '"') {
      quoted = true
    } else if (char === delimiter) {
      row.push(cell)
      cell = ''
    } else if (char === '\n') {
      row.push(cell)
      rows.push(row)
      row = []
      cell = ''
    } else if (char !== '\r') {
      cell += char
    }
  }
  if (cell !== '' || row.length > 0) {
    row.push(cell)
    rows.push(row)
  }
  return rows
}

interface Props {
  url: string
  name: string
  kind: 'csv' | 'sheet'
}

/** 表格预览：CSV/TSV 直接解析，xlsx/xls/ods 用 SheetJS 读取，多工作表可切换。 */
export function SpreadsheetPreview({ url, name, kind }: Props) {
  const [sheets, setSheets] = useState<Sheet[]>([])
  const [active, setActive] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(null)
    setSheets([])
    setActive(0)

    const load = async () => {
      if (kind === 'csv') {
        const response = await fetch(url)
        if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`)
        const text = await response.text()
        const delimiter = /\.tsv$/i.test(name) ? '\t' : ','
        return [{ name: '表格', rows: parseDelimited(text, delimiter) }]
      }
      const XLSX = await import('xlsx')
      const response = await fetch(url)
      if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`)
      const buffer = await response.arrayBuffer()
      const book = XLSX.read(buffer, { type: 'array' })
      return book.SheetNames.map((name) => {
        const sheet = book.Sheets[name]
        const raw = XLSX.utils.sheet_to_json<unknown[]>(sheet, {
          header: 1,
          blankrows: false,
          raw: false,
          defval: '',
        })
        return {
          name,
          rows: raw.map((row) => (row as unknown[]).map((cell) => String(cell ?? ''))),
        }
      })
    }

    load()
      .then((next) => {
        if (alive) setSheets(next)
      })
      .catch((loadError: unknown) => {
        if (alive) {
          setError(loadError instanceof Error ? loadError.message : '表格读取失败')
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [url, kind, name])

  const sheet = sheets[active]
  const { rows, columns, truncatedRows, truncatedCols } = useMemo(() => {
    const all = sheet?.rows ?? []
    const width = all.reduce((max, row) => Math.max(max, row.length), 0)
    const cols = Math.min(width, MAX_COLS)
    return {
      rows: all.slice(0, MAX_ROWS),
      columns: cols,
      truncatedRows: all.length > MAX_ROWS,
      truncatedCols: width > MAX_COLS,
    }
  }, [sheet])

  if (loading) return <p className="project-preview-note">正在读取表格…</p>
  if (error) return <p className="project-preview-note">{error}</p>
  if (!sheet) return <p className="project-preview-note">表格里没有可显示的内容。</p>

  const header = rows[0] ?? []
  const body = rows.slice(1)

  return (
    <div className="sheet-preview">
      {sheets.length > 1 && (
        <div className="sheet-preview-tabs">
          {sheets.map((item, index) => (
            <button
              key={item.name}
              type="button"
              data-active={index === active}
              onClick={() => setActive(index)}
            >
              {item.name}
            </button>
          ))}
        </div>
      )}

      <div className="sheet-preview-scroll">
        <table>
          <thead>
            <tr>
              <th className="sheet-preview-index" />
              {Array.from({ length: columns }, (_, index) => (
                <th key={index}>{header[index] || `列 ${index + 1}`}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, rowIndex) => (
              <tr key={rowIndex}>
                <td className="sheet-preview-index">{rowIndex + 1}</td>
                {Array.from({ length: columns }, (_, colIndex) => (
                  <td key={colIndex}>{row[colIndex] ?? ''}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="sheet-preview-note">
        {body.length} 行 × {columns} 列
        {truncatedRows || truncatedCols
          ? ` · 仅显示前 ${MAX_ROWS} 行${truncatedCols ? `、前 ${MAX_COLS} 列` : ''}`
          : ''}
      </p>
    </div>
  )
}
