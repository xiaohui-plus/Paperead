export type FileFormat =
  | 'pdf'
  | 'word'
  | 'table'
  | 'powerpoint'
  | 'markdown'
  | 'mdx'
  | 'document'
  | 'log'
  | 'citation'
  | 'tex'
  | 'epub'
  | 'email'
  | 'font'
  | 'lock'
  | 'diff'
  | 'exe'
  | 'dll'
  | 'jar'
  | 'android'
  | 'zip'
  | 'file'
  | 'json'
  | 'yaml'
  | 'toml'
  | 'xml'
  | 'settings'
  | 'key'
  | 'git'
  | 'database'
  | 'html'
  | 'css'
  | 'sass'
  | 'less'
  | 'image'
  | 'svg'
  | 'audio'
  | 'video'
  | 'subtitle'
  | '3d'
  | 'javascript'
  | 'typescript'
  | 'react'
  | 'react-ts'
  | 'vue'
  | 'svelte'
  | 'python'
  | 'jupyter'
  | 'r'
  | 'julia'
  | 'matlab'
  | 'c'
  | 'h'
  | 'cpp'
  | 'csharp'
  | 'go'
  | 'rust'
  | 'java'
  | 'kotlin'
  | 'swift'
  | 'php'
  | 'ruby'
  | 'perl'
  | 'lua'
  | 'console'
  | 'powershell'

const EXTENSIONS: Record<string, FileFormat> = {
  pdf: 'pdf',
  doc: 'word',
  docx: 'word',
  docm: 'word',
  dot: 'word',
  dotx: 'word',
  odt: 'word',
  rtf: 'word',
  pages: 'word',
  wps: 'word',
  xls: 'table',
  xlsx: 'table',
  xlsm: 'table',
  xlsb: 'table',
  ods: 'table',
  numbers: 'table',
  csv: 'table',
  tsv: 'table',
  ppt: 'powerpoint',
  pptx: 'powerpoint',
  pptm: 'powerpoint',
  pps: 'powerpoint',
  ppsx: 'powerpoint',
  odp: 'powerpoint',
  key: 'powerpoint',
  md: 'markdown',
  markdown: 'markdown',
  mdown: 'markdown',
  mkd: 'markdown',
  mdx: 'mdx',
  txt: 'document',
  text: 'document',
  nfo: 'document',
  log: 'log',
  bib: 'citation',
  bibtex: 'citation',
  ris: 'citation',
  enw: 'citation',
  nbib: 'citation',
  tex: 'tex',
  latex: 'tex',
  cls: 'tex',
  sty: 'tex',
  json: 'json',
  jsonl: 'json',
  ndjson: 'json',
  json5: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  xml: 'xml',
  xsl: 'xml',
  xslt: 'xml',
  plist: 'xml',
  html: 'html',
  htm: 'html',
  xhtml: 'html',
  css: 'css',
  scss: 'sass',
  sass: 'sass',
  less: 'less',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  mts: 'typescript',
  cts: 'typescript',
  jsx: 'react',
  tsx: 'react-ts',
  vue: 'vue',
  svelte: 'svelte',
  py: 'python',
  pyw: 'python',
  pyi: 'python',
  ipynb: 'jupyter',
  r: 'r',
  rmd: 'r',
  jl: 'julia',
  m: 'matlab',
  c: 'c',
  h: 'h',
  cpp: 'cpp',
  cxx: 'cpp',
  cc: 'cpp',
  hpp: 'cpp',
  hxx: 'cpp',
  cs: 'csharp',
  go: 'go',
  rs: 'rust',
  java: 'java',
  kt: 'kotlin',
  kts: 'kotlin',
  swift: 'swift',
  php: 'php',
  rb: 'ruby',
  rake: 'ruby',
  gemspec: 'ruby',
  pl: 'perl',
  pm: 'perl',
  lua: 'lua',
  sh: 'console',
  bash: 'console',
  zsh: 'console',
  fish: 'console',
  bat: 'console',
  cmd: 'console',
  ps1: 'powershell',
  psm1: 'powershell',
  psd1: 'powershell',
  sql: 'database',
  db: 'database',
  sqlite: 'database',
  sqlite3: 'database',
  png: 'image',
  jpg: 'image',
  jpeg: 'image',
  gif: 'image',
  webp: 'image',
  bmp: 'image',
  tif: 'image',
  tiff: 'image',
  avif: 'image',
  heic: 'image',
  heif: 'image',
  jfif: 'image',
  ico: 'image',
  svg: 'svg',
  mp3: 'audio',
  wav: 'audio',
  ogg: 'audio',
  oga: 'audio',
  m4a: 'audio',
  flac: 'audio',
  aac: 'audio',
  opus: 'audio',
  wma: 'audio',
  mp4: 'video',
  webm: 'video',
  mov: 'video',
  mkv: 'video',
  m4v: 'video',
  avi: 'video',
  wmv: 'video',
  flv: 'video',
  mpeg: 'video',
  mpg: 'video',
  srt: 'subtitle',
  vtt: 'subtitle',
  ass: 'subtitle',
  ssa: 'subtitle',
  sub: 'subtitle',
  zip: 'zip',
  rar: 'zip',
  '7z': 'zip',
  tar: 'zip',
  gz: 'zip',
  bz2: 'zip',
  xz: 'zip',
  tgz: 'zip',
  zst: 'zip',
  jar: 'jar',
  apk: 'android',
  exe: 'exe',
  dll: 'dll',
  so: 'dll',
  dylib: 'dll',
  ttf: 'font',
  otf: 'font',
  woff: 'font',
  woff2: 'font',
  eot: 'font',
  epub: 'epub',
  mobi: 'epub',
  azw: 'epub',
  azw3: 'epub',
  fb2: 'epub',
  eml: 'email',
  msg: 'email',
  lock: 'lock',
  diff: 'diff',
  patch: 'diff',
  obj: '3d',
  stl: '3d',
  fbx: '3d',
  gltf: '3d',
  glb: '3d',
  blend: '3d',
  '3dm': '3d',
  cfg: 'settings',
  ini: 'settings',
  conf: 'settings',
  config: 'settings',
  properties: 'settings',
  env: 'key',
}

const MIME_MAP: Array<[string, FileFormat]> = [
  ['application/pdf', 'pdf'],
  ['application/msword', 'word'],
  ['application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'word'],
  ['application/vnd.oasis.opendocument.text', 'word'],
  ['application/rtf', 'word'],
  ['text/rtf', 'word'],
  ['application/vnd.ms-excel', 'table'],
  ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'table'],
  ['application/vnd.oasis.opendocument.spreadsheet', 'table'],
  ['text/csv', 'table'],
  ['text/tab-separated-values', 'table'],
  ['application/vnd.ms-powerpoint', 'powerpoint'],
  ['application/vnd.openxmlformats-officedocument.presentationml.presentation', 'powerpoint'],
  ['application/vnd.oasis.opendocument.presentation', 'powerpoint'],
  ['text/markdown', 'markdown'],
  ['text/x-markdown', 'markdown'],
  ['application/epub+zip', 'epub'],
  ['application/vnd.amazon.ebook', 'epub'],
  ['application/x-bibtex', 'citation'],
  ['text/x-bibtex', 'citation'],
  ['application/x-tex', 'tex'],
  ['text/x-tex', 'tex'],
  ['application/json', 'json'],
  ['application/ld+json', 'json'],
  ['application/yaml', 'yaml'],
  ['text/yaml', 'yaml'],
  ['application/x-yaml', 'yaml'],
  ['application/toml', 'toml'],
  ['application/xml', 'xml'],
  ['text/xml', 'xml'],
  ['text/html', 'html'],
  ['text/css', 'css'],
  ['application/javascript', 'javascript'],
  ['text/javascript', 'javascript'],
  ['application/x-javascript', 'javascript'],
  ['application/typescript', 'typescript'],
  ['text/typescript', 'typescript'],
  ['application/x-python', 'python'],
  ['text/x-python', 'python'],
  ['application/x-sh', 'console'],
  ['text/x-shellscript', 'console'],
  ['application/x-powershell', 'powershell'],
  ['application/sql', 'database'],
  ['application/zip', 'zip'],
  ['application/x-zip-compressed', 'zip'],
  ['application/x-rar-compressed', 'zip'],
  ['application/x-7z-compressed', 'zip'],
  ['application/gzip', 'zip'],
  ['application/x-tar', 'zip'],
  ['application/java-archive', 'jar'],
  ['application/vnd.android.package-archive', 'android'],
  ['application/x-msdownload', 'exe'],
  ['application/vnd.ms-fontobject', 'font'],
  ['application/font-woff', 'font'],
  ['application/font-woff2', 'font'],
  ['application/font-sfnt', 'font'],
  ['image/svg+xml', 'svg'],
]

const SPECIAL_NAMES: Record<string, FileFormat> = {
  dockerfile: 'settings',
  'docker-compose.yml': 'settings',
  'docker-compose.yaml': 'settings',
  makefile: 'console',
  'cmakelists.txt': 'settings',
  license: 'document',
  notice: 'document',
  readme: 'markdown',
  changelog: 'markdown',
  '.gitignore': 'git',
  '.gitattributes': 'git',
  '.gitmodules': 'git',
  '.gitkeep': 'git',
  '.env': 'key',
}

function extensionOf(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? name
  const index = base.lastIndexOf('.')
  if (index <= 0 || index >= base.length - 1) return ''
  return base.slice(index + 1).toLowerCase()
}

function mimeFormat(mime: string): FileFormat | null {
  for (const [key, format] of MIME_MAP) {
    if (mime === key) return format
  }
  if (mime.startsWith('image/')) return 'image'
  if (mime.startsWith('audio/')) return 'audio'
  if (mime.startsWith('video/')) return 'video'
  if (mime.startsWith('font/')) return 'font'
  if (mime.startsWith('text/')) return 'document'
  return null
}

export function fileFormatOf(name: string, mime?: string): FileFormat {
  const base = (name.split(/[\\/]/).pop() ?? name).toLowerCase()
  const special = SPECIAL_NAMES[base]
  if (special) return special
  if (base.startsWith('.env.')) return 'key'
  const extension = extensionOf(base)
  if (extension) {
    const known = EXTENSIONS[extension]
    if (known) return known
  }
  const normalizedMime = (mime ?? '').toLowerCase()
  if (normalizedMime) {
    const known = mimeFormat(normalizedMime)
    if (known) return known
  }
  return 'file'
}