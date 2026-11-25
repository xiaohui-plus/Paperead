/** 把 mermaid 图里的 `$...$` 标签用 KaTeX 重新排版。
 *
 * mermaid 本身不支持 LaTeX，节点/边标签里的公式会被当成普通文字输出。
 * 这里在 SVG 渲染完成后，找到含公式的 <text>，用 foreignObject + KaTeX
 * 覆盖原文字，保留原有坐标与居中方式。
 */

import { renderMathHtml } from './math'

const MATH_RE = /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (char) => ESCAPES[char] ?? char)
}

function labelHtml(text: string): string {
  MATH_RE.lastIndex = 0
  let out = ''
  let last = 0
  let matched = false
  let match: RegExpExecArray | null

  while ((match = MATH_RE.exec(text)) !== null) {
    matched = true
    out += escapeHtml(text.slice(last, match.index))
    const latex = (match[1] ?? match[2] ?? '').trim()
    out += renderMathHtml(latex) ?? escapeHtml(`$${latex}$`)
    last = match.index + match[0].length
  }
  out += escapeHtml(text.slice(last))
  return matched ? out : escapeHtml(text)
}

function hasMath(text: string): boolean {
  MATH_RE.lastIndex = 0
  return MATH_RE.test(text)
}

/** 按 tspan 的 y 把多行标签拆成行，保留 mermaid 的换行结构。 */
function labelRows(text: SVGGraphicsElement): string[] {
  const tspans = Array.from(text.querySelectorAll('tspan'))
  if (!tspans.length) return [text.textContent ?? '']

  const rows: string[] = []
  let currentY: string | null = null
  for (const tspan of tspans) {
    const y = tspan.getAttribute('y') ?? tspan.getAttribute('dy') ?? ''
    if (currentY === null || y !== currentY) {
      rows.push(tspan.textContent ?? '')
      currentY = y
    } else {
      rows[rows.length - 1] += tspan.textContent ?? ''
    }
  }
  return rows.filter((row) => row.trim().length > 0)
}

function upgradeLabel(text: SVGGraphicsElement, svg: SVGSVGElement): void {
  const rows = labelRows(text)
  if (!rows.some(hasMath)) return

  const bbox = text.getBBox()
  if (!bbox.width || !bbox.height) return

  const computed = window.getComputedStyle(text)
  const fontSize = parseFloat(computed.fontSize) || 12
  const anchor = computed.textAnchor || 'start'

  const div = document.createElement('div')
  div.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml')
  div.style.cssText = [
    'display:flex',
    'flex-direction:column',
    'align-items:center',
    'justify-content:center',
    'gap:0.08em',
    `font-size:${fontSize}px`,
    'line-height:1.25',
    'white-space:nowrap',
    `color:${computed.fill || '#333'}`,
  ].join(';')
  div.innerHTML = rows.map((row) => `<div>${labelHtml(row)}</div>`).join('')

  const probe = div.cloneNode(true) as HTMLDivElement
  probe.style.position = 'absolute'
  probe.style.visibility = 'hidden'
  probe.style.left = '-9999px'
  document.body.appendChild(probe)
  const width = Math.max(probe.scrollWidth, bbox.width)
  const height = Math.max(probe.scrollHeight, bbox.height)
  probe.remove()

  let x = bbox.x
  if (anchor === 'middle') x = bbox.x + bbox.width / 2 - width / 2
  else if (anchor === 'end') x = bbox.x + bbox.width - width
  const y = bbox.y + bbox.height / 2 - height / 2

  text.style.visibility = 'hidden'

  const foreign = document.createElementNS('http://www.w3.org/2000/svg', 'foreignObject')
  foreign.setAttribute('x', String(x))
  foreign.setAttribute('y', String(y))
  foreign.setAttribute('width', String(width))
  foreign.setAttribute('height', String(height))
  foreign.setAttribute('overflow', 'visible')
  foreign.appendChild(div)
  text.parentNode?.insertBefore(foreign, text.nextSibling)

  svg.style.overflow = 'visible'
}

/** mermaid 的节点标签走 HTML（foreignObject + <p>），单独替换其中的公式。 */
function upgradeForeignLabels(root: SVGSVGElement): void {
  const labels = root.querySelectorAll<HTMLElement>('foreignObject p')
  for (const label of labels) {
    const html = label.innerHTML
    if (!html.includes('$')) continue
    label.innerHTML = html.replace(/\$([^$\n]+?)\$/g, (_full, latex: string) =>
      renderMathHtml(latex.trim()) ?? escapeHtml(`$${latex.trim()}$`),
    )
  }
}

export function upgradeSvgMath(svg: SVGSVGElement | null): void {
  if (!svg) return
  upgradeForeignLabels(svg)
  const labels = Array.from(svg.querySelectorAll<SVGGraphicsElement>('text'))
  for (const label of labels) {
    try {
      upgradeLabel(label, svg)
    } catch {
      // 单个标签失败不影响整张图，保持 mermaid 原文
    }
  }
}