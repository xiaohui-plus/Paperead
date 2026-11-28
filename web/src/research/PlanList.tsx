import { IconCheck } from '../components/icons'
import { asText, type PlanItem } from '../lib/researchTypes'

function planLabel(item: PlanItem, index: number): string {
  return (
    asText(item.content) ||
    asText(item.title) ||
    asText(item.text) ||
    (typeof item.step === 'string' ? item.step : '') ||
    `步骤 ${index + 1}`
  )
}

export function PlanList({ items }: { items: PlanItem[] }) {
  return (
    <ul className="plan-list">
      {items.map((item, index) => (
        <li key={String(item.id ?? index)} data-status={String(item.status || '')}>
          <span className="plan-mark">
            {item.status === 'completed' ? <IconCheck /> : null}
          </span>
          <span>{planLabel(item, index)}</span>
        </li>
      ))}
    </ul>
  )
}