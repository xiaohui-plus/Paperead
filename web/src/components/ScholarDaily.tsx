import { ScholarResult } from './ScholarResult'
import type { DailyResponse, ScholarPaper } from '../lib/scholar'

interface Props {
  data: DailyResponse | null
  loading: boolean
  onRound: () => void
  onOpen: (paper: ScholarPaper) => void
}

/** 无关键词时的默认内容：每日推荐。 */
export function ScholarDaily({ data, loading, onRound, onOpen }: Props) {
  return (
    <section className="scholar-daily">
      <div className="scholar-daily-head">
        <div>
          <h2>每日推荐</h2>
          <p>
            {data?.date ? `${data.date} · ` : ''}
            人工智能与机器学习 / 生物医学与交叉
          </p>
        </div>
        <button className="btn ghost" onClick={onRound} disabled={loading}>
          {loading ? '加载中…' : '换一批'}
        </button>
      </div>

      {data?.error && <p className="scholar-empty">{data.error}</p>}
      {!data && !loading && <p className="scholar-empty">正在挑选今日论文…</p>}
      {loading && !data && <p className="scholar-empty">正在挑选今日论文…</p>}

      <div className={`scholar-daily-grid ${loading ? 'is-loading' : ''}`}>
        {data?.papers.map((paper) => (
          <ScholarResult key={paper.id} paper={paper} onOpen={onOpen} />
        ))}
      </div>
    </section>
  )
}
