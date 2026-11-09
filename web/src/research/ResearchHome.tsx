import { useState } from 'react'
import { IconList, IconSend, IconSpark } from '../components/icons'
import type { ProjectKind, ProjectSummary } from '../lib/researchApi'
import type { StartHandler } from './ResearchShell'

interface Props {
  kind: ProjectKind
  projects: ProjectSummary[]
  onStart: StartHandler
}

const RESEARCH_ACTIONS = [
  [
    '找文献',
    '从本地库与学术库找可读的论文',
    '帮我找文献：先用本地文献库，再用学术检索（每次取 20-30 条带摘要的结果）。以摘要筛选为主，不要下载 PDF；给出 5-8 篇最相关的论文，逐篇写标题、年份、来源和基于摘要的一句话相关性，摘要缺失的条目要标注。只有我明确说“精读/下载”时，才把论文导入项目论文文件夹。',
  ],
  [
    '整理文献',
    '去重、聚类、打标签',
    '帮我整理文献：检查本地文献库里重复或高度相似的条目，按主题聚类，给每篇打 1-3 个标签，并输出一份整理结果（合并了哪些、每类的代表论文、还缺什么）。',
  ],
  [
    '定研究问题',
    '收敛成可验证的问题',
    '帮我定研究问题：基于当前项目和文献库，提出 3 个可验证的研究问题。每个写清研究对象、关键变量或干预、对照、可测结局，并说明需要什么数据才能回答；最后推荐一个最值得先做的。',
  ],
  [
    '设计研究',
    '方法、数据与对照',
    '帮我设计研究：针对当前研究问题给出研究设计，包含研究类型、样本与纳排标准、分组或对照、主要与次要终点、统计方法、偏倚控制与伦理要点，并指出最大的可行性风险。',
  ],
  [
    '写作修改',
    '按证据重写段落',
    '帮我写作修改：基于当前项目和文献库中的证据重写一段核心论述（若我没指定就先挑最关键的一段）。要求每个结论都能追溯到具体文献，标出引用，并指出哪些句子目前没有证据支持。',
  ],
  [
    '做汇报',
    '组会与答辩提纲',
    '帮我做汇报：把当前研究整理成 10 分钟组会汇报，包含背景、科学问题、方法、主要结果、局限与下一步；每页给 1-2 条要点，并标出还需要补的实验或文献。',
  ],
] as const

const REVIEW_ACTIONS = [
  [
    '列大纲',
    '先定章节与每节要回答的问题',
    '帮我列综述大纲：围绕当前综述主题给出章节结构，每一节写清要回答的核心问题、需要的证据类型和预期图表，并指出证据可能不足的部分。',
  ],
  [
    '找证据',
    '按小节补可核查来源',
    '帮我找证据：按综述大纲逐节检索可核查来源（本地文献库 + 学术检索），列出每节可用的引用、关键结论和页码，找不到证据的地方明确标出来。',
  ],
  [
    '写初稿',
    '逐节起草，引用可追溯',
    '帮我写综述初稿：按大纲逐节起草，每个论断标注可追溯的引用（论文 + 页码），未找到证据的论断用【待补证据】标出，不要编造参考文献。',
  ],
  [
    '核引用',
    '逐条核验并修正',
    '帮我核引用：逐条检查草稿中的引用是否真实存在、是否支持该句结论，给出保留 / 替换 / 删除的建议，并列出所有查无实据的引用。',
  ],
  [
    '转格式',
    '导出 docx / bib',
    '帮我转格式：把当前综述导出为 docx 与 bib 文件，写到项目文件里，并告诉我文件路径和包含的章节。',
  ],
  [
    '做汇报',
    '把综述压成汇报提纲',
    '帮我做汇报：把当前综述压缩成 10 分钟汇报提纲，包含背景、检索与筛选方法、主要结论、证据强度、争议与未来方向；每页给 1-2 条要点。',
  ],
] as const

const RESEARCH_TOPICS = [
  '肝癌免疫治疗耐药机制',
  '大模型在医学影像中的应用',
  'CRISPR 递送系统',
  '单细胞测序与肿瘤微环境',
]

const REVIEW_EXAMPLES = [
  ['近五年肝癌免疫治疗进展', '从机制到临床终点，梳理证据链'],
  ['CRISPR 递送系统综述', '病毒与非病毒载体的取舍'],
  ['大模型在医学影像中的应用', '数据、评测与落地边界'],
] as const

/** 研究与写综述的功能首页：项目选择 + composer + 快捷动作。 */
export function ResearchHome({ kind, projects, onStart }: Props) {
  const [target, setTarget] = useState<string>('auto')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  // 手机端回车是换行键，不劫持成发送。
  const [touch] = useState(
    () =>
      typeof window !== 'undefined' &&
      Boolean(window.matchMedia?.('(pointer: coarse)').matches),
  )

  const effectiveProject =
    target === 'new' ? null : target === 'auto' ? (projects[0]?.id ?? null) : target

  const run = async (text: string, title: string) => {
    const value = text.trim()
    if (!value || busy) return
    setBusy(true)
    try {
      await onStart(value, title, effectiveProject)
      setDraft('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="research-home">
      <header className="research-hero">
        <span className="research-kicker">
          {kind === 'research' ? <IconSpark /> : <IconList />}
          {kind === 'research' ? '研究' : '写综述报告'}
        </span>
        <h1>
          {kind === 'research'
            ? '快速开始，或切换项目继续研究'
            : '今天想研究点什么？'}
        </h1>
        <p>
          {kind === 'research'
            ? '从文献库或学术库出发，检索、精读、整理，再让 agent 接着往下做。'
            : '先定问题，再列证据，最后逐节写成可核验的综述草稿。'}
        </p>
      </header>

      <section className="research-composer">
        <div className="research-composer-top">
          <label>
            在项目
            <select
              value={target}
              onChange={(event) => setTarget(event.target.value)}
            >
              <option value="auto">
                {projects[0] ? `最近：${projects[0].name}` : '新项目（自动创建）'}
              </option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
              <option value="new">新项目</option>
            </select>
          </label>
          <span className="research-composer-hint">
            发送后自动建会话，可随时回到项目继续
          </span>
        </div>
        <textarea
          value={draft}
          placeholder={
            kind === 'research'
              ? '描述你想研究的问题，或 @ 一份文献、$ 调用一个技能…'
              : '描述综述主题、时间范围与目标读者…'
          }
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !touch) {
              event.preventDefault()
              void run(draft, draft.slice(0, 40))
            }
          }}
        />
        <div className="research-composer-actions">
          <span>
            {touch ? '点「开始」发送，回车换行' : 'Enter 发送 · Shift+Enter 换行'}
          </span>
          <button
            className="research-send"
            disabled={busy || !draft.trim()}
            onClick={() => void run(draft, draft.slice(0, 40))}
          >
            <IconSend />
            {busy ? '创建中…' : '开始'}
          </button>
        </div>
      </section>

      <section className="research-section">
        <h2>快捷开始</h2>
        <div className="research-actions">
          {(kind === 'research' ? RESEARCH_ACTIONS : REVIEW_ACTIONS).map(
            ([label, description, prompt]) => (
              <button
                key={label}
                className="research-action"
                disabled={busy}
                onClick={() => void run(prompt, label)}
              >
                <strong>{label}</strong>
                <span>{description}</span>
              </button>
            ),
          )}
        </div>
      </section>

      {kind === 'research' ? (
        <section className="research-section">
          <h2>大家都在搜</h2>
          <div className="research-topics">
            {RESEARCH_TOPICS.map((topic) => (
              <button
                key={topic}
                className="research-topic"
                disabled={busy}
                onClick={() =>
                  void run(
                    `我想研究「${topic}」。请先检索本地文献库和学术检索，给出 5-8 篇关键论文与 3 个可研究的问题，并推荐先从哪个问题入手。`,
                    topic,
                  )
                }
              >
                {topic}
              </button>
            ))}
          </div>
        </section>
      ) : (
        <section className="research-section">
          <h2>综述报告示例</h2>
          <div className="research-actions">
            {REVIEW_EXAMPLES.map(([label, description]) => (
              <button
                key={label}
                className="research-action"
                data-review
                disabled={busy}
                onClick={() =>
                  void run(
                    `帮我写一篇综述：「${label}」——${description}。先列大纲与证据缺口，再逐节起草，引用必须可追溯；查不到证据的地方明确标注。`,
                    label,
                  )
                }
              >
                <strong>{label}</strong>
                <span>{description}</span>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
