const LEAD_HEADING = /^(?:[ \t]*\r?\n)*[ \t]*#[ \t]+([^\r\n]*)(\r?\n)?/

export interface LeadHeading {
  title: string
  body: string
  complete: boolean
}

function headingTitle(raw: string): string {
  return raw
    .replace(/[ \t]+#+[ \t]*$/, '')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/__(.+?)__/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .trim()
}

export function splitLeadHeading(text: string): LeadHeading | null {
  if (!text) return null
  const match = text.match(LEAD_HEADING)
  if (!match) return null
  const title = headingTitle(match[1])
  if (!title) return null
  return {
    title,
    body: text.slice(match[0].length),
    complete: Boolean(match[2]),
  }
}