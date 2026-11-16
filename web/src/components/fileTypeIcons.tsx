import type { ReactElement } from 'react'
import { fileFormatOf, type FileFormat } from './fileFormat'

const RAW_ICONS = import.meta.glob('../assets/file-icons/*.svg', {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>

const ICONS: Record<string, string> = {}
for (const [path, raw] of Object.entries(RAW_ICONS)) {
  const name = path.split('/').pop()?.replace(/\.svg$/i, '')
  if (name) ICONS[name] = raw
}

const ELEMENTS = new Map<FileFormat, ReactElement>()

function iconElement(format: FileFormat): ReactElement {
  const cached = ELEMENTS.get(format)
  if (cached) return cached
  const raw = ICONS[format] ?? ICONS.file ?? ''
  const element = (
    <span
      className="file-type-icon"
      data-format={format}
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: raw }}
    />
  )
  ELEMENTS.set(format, element)
  return element
}

interface Props {
  name: string
  mime?: string
}

export function FileTypeIcon({ name, mime }: Props) {
  return iconElement(fileFormatOf(name, mime))
}