import { useRef, useState } from 'react'
import { IconList, IconSend, IconSpark } from '../components/icons'
import type { ProjectKind, ProjectSummary } from '../lib/researchApi'
import { tourBus } from '../tour/tourBus'
import type { StartHandler } from './ResearchShell'

interface Props {
  kind: ProjectKind
  projects: ProjectSummary[]
  onStart: StartHandler
}

const RESEARCH_ACTIONS = [
  [
    '找文献',
    '从本地库与学术库找可读论文',
    '帮我在「肝癌免疫治疗耐药」方向找 8 篇近五年的核心论文：先用本地文献库，再用学术检索补充；每篇给出标题、年份、期刊或会议、基于摘要的两句相关性说明，并标注能否获取全文。先不要下载 PDF。',
  ],
  [
    '整理文献',
    '去重、聚类并标出证据缺口',
    '帮我整理本地文献库中与「肝癌免疫治疗」相关的论文：合并重复和高度相似的条目，按耐药机制、生物标志物、联合治疗等主题聚类，每篇打 1–3 个标签；最后输出一张表，列出聚类、代表论文、年份和可用证据类型，并指出还缺哪类研究。',
  ],
  [
    '定研究问题',
    '收敛成可验证的问题',
    '基于本地文献库中「肝癌免疫治疗耐药」的论文，帮我提出 3 个可验证的研究问题。每个写清研究对象、关键变量或干预、对照、主要结局和所需数据；再比较三者的创新性与可行性，推荐先做哪一个。',
  ],
  [
    '设计研究',
    '方法、对照与失败风险',
    '针对「PD-1 抑制剂耐药后的联合治疗策略」设计一项可执行的研究：给出研究类型、样本量与纳排标准、分组与对照、主要和次要终点、统计方法、偏倚控制；再列出最可能失败的三个环节及应对办法。',
  ],
  [
    '写作修改',
    '按证据重写段落',
    '帮我把「讨论部分的耐药机制段落」改写成论文风格：保留原意和引用，按“现象—证据—解释—局限”的顺序重组；每个结论后标出来源，把没有证据支撑的句子单独列出来。',
  ],
  [
    '做汇报',
    '组会与答辩 PPT',
    '把「肝癌免疫治疗耐药」这个课题整理成 10 分钟组会汇报：背景 2 页、问题与方法 3 页、主要结果 3 页、局限与下一步 2 页；每页 1–2 条要点，并附一句讲稿，最后生成 .pptx。',
  ],
] as const

const REVIEW_ACTIONS = [
  [
    '列大纲',
    '先定章节与每节要回答的问题',
    '帮我写一篇关于「图神经网络在药物发现中的应用」的综述，先只列大纲：按“背景—任务分类—方法比较—评测与数据—挑战与展望”组织，每节写清要回答的问题、需要的证据类型和 1–2 个预期图表，并标出可能缺证据的章节。',
  ],
  [
    '找证据',
    '按小节补可核查来源',
    '按刚才的大纲逐节找证据：优先用本地文献库的全文，不足再用学术检索补充；每节列出 3–5 条可引用来源（作者、年份、页码或 DOI）和对应结论，找不到证据的地方明确写“缺证据”。',
  ],
  [
    '写初稿',
    '逐节起草，引用可追溯',
    '按大纲写综述初稿：每节先给结论再给证据；比较不同研究时写清数据集、评价指标和结果差异；每个论断标注来源，查不到证据的用【待补证据】标出，不要编造参考文献。',
  ],
  [
    '核引用',
    '逐条核验并给出处理建议',
    '帮我核对这篇综述的引用：逐条检查文献是否存在、是否支持所在句子，按“保留/替换/删除”给出处理建议；重点检查结论段和数字旁边的引用。',
  ],
  [
    '转格式',
    '导出 docx / bib',
    '把定稿导出为 docx 和 bib：正文用编号引用并与参考文献一一对应，图表带编号和标题；同时给我一份导出清单，写明文件路径、章节数和文献条数。',
  ],
  [
    '做汇报',
    '把综述压成组会 PPT',
    '把这篇综述压缩成 10 分钟汇报：背景 2 页、检索与筛选方法 2 页、主要结论 3 页、争议与局限 2 页、展望 1 页；每页 1–2 条要点，并生成 .pptx。',
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
  const [filled, setFilled] = useState(false)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)
  // 手机端回车是换行键，不劫持成发送。
  const [touch] = useState(
    () =>
      typeof window !== 'undefined' &&
      Boolean(window.matchMedia?.('(pointer: coarse)').matches),
  )

  const effectiveProject =
    target === 'new' ? null : target === 'auto' ? (projects[0]?.id ?? null) : target

  /** 快捷卡片只把示例填进输入框，等用户改完再自己发送。 */
  const fillDraft = (text: string) => {
    setDraft(text)
    setFilled(true)
    requestAnimationFrame(() => {
      const input = composerRef.current
      if (!input) return
      input.focus()
      input.setSelectionRange(text.length, text.length)
      input.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    })
  }

  const run = async (text: string, title: string) => {
    const value = text.trim()
    if (!value || busy) return
    setBusy(true)
    try {
      tourBus.emit('research:action', { label: title })
      await onStart(value, title, effectiveProject)
      setDraft('')
      setFilled(false)
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
            {filled
              ? '示例已填入，可修改后发送'
              : '发送后自动建会话，可随时回到项目继续'}
          </span>
        </div>
        <textarea
          ref={composerRef}
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

      <section
        className="research-section"
        data-tour={kind === 'research' ? 'research-actions' : 'review-actions'}
      >
        <h2>快捷开始</h2>
        <div className="research-actions">
          {(kind === 'research' ? RESEARCH_ACTIONS : REVIEW_ACTIONS).map(
            ([label, description, prompt]) => (
              <button
                key={label}
                className="research-action"
                onClick={() => fillDraft(prompt)}
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
                onClick={() =>
                  fillDraft(
                    `我想研究「${topic}」。请先检索本地文献库和学术检索，给出 5–8 篇关键论文和 3 个可研究的问题，并说明每个问题需要什么数据才能回答。`,
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
                onClick={() =>
                  fillDraft(
                    `帮我写一篇综述：「${label}」——${description}。先列大纲与证据缺口，再逐节起草；引用必须可追溯到具体文献和页码。`,
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
