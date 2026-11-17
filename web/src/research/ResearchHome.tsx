import { useState } from 'react'
import { IconClose } from '../components/icons'
import type { ProjectKind, ProjectSummary } from '../lib/researchApi'
import { tourBus } from '../tour/tourBus'
import { Composer } from './Composer'
import type { CreateProjectHandler, StartHandler } from './ResearchShell'
import { ProjectPicker } from './ProjectPicker'

interface Props {
  kind: ProjectKind
  projects: ProjectSummary[]
  onStart: StartHandler
  onCreateProject: CreateProjectHandler
  onOpenSettings: () => void
}

interface HomeExample {
  tag: string
  text: string
}

const RESEARCH_EXAMPLES: HomeExample[] = [
  {
    tag: '找文献',
    text: '帮我在「肝癌免疫治疗耐药」方向找 8 篇近五年的核心论文：先用本地文献库，再用学术检索补充；每篇给出标题、年份、期刊或会议、基于摘要的两句相关性说明，并标注能否获取全文。先不要下载 PDF。',
  },
  {
    tag: '定研究问题',
    text: '基于本地文献库中「肝癌免疫治疗耐药」的论文，帮我提出 3 个可验证的研究问题。每个写清研究对象、关键变量或干预、对照、主要结局和所需数据；再比较三者的创新性与可行性，推荐先做哪一个。',
  },
  {
    tag: '设计研究',
    text: '针对「PD-1 抑制剂耐药后的联合治疗策略」设计一项可执行的研究：给出研究类型、样本量与纳排标准、分组与对照、主要和次要终点、统计方法、偏倚控制；再列出最可能失败的三个环节及应对办法。',
  },
]

const REVIEW_EXAMPLES: HomeExample[] = [
  {
    tag: '列大纲',
    text: '帮我写一篇关于「图神经网络在药物发现中的应用」的综述，先只列大纲：按“背景—任务分类—方法比较—评测与数据—挑战与展望”组织，每节写清要回答的问题、需要的证据类型和 1–2 个预期图表。',
  },
  {
    tag: '找证据',
    text: '按刚才的大纲逐节找证据：优先用本地文献库的全文，不足再用学术检索补充；每节列出 3–5 条可引用来源（作者、年份、页码或 DOI）和对应结论，找不到证据的地方明确写“缺证据”。',
  },
  {
    tag: '写初稿',
    text: '按大纲写综述初稿：每节先给结论再给证据；比较不同研究时写清数据集、评价指标和结果差异；每个论断标注来源，查不到证据的用【待补证据】标出，不要编造参考文献。',
  },
]

/** 研究与写综述的功能首页：一句提问 + 消息框 + 示例。 */
export function ResearchHome({
  kind,
  projects,
  onStart,
  onCreateProject,
  onOpenSettings,
}: Props) {
  const [target, setTarget] = useState<string>('auto')
  const [busy, setBusy] = useState(false)
  const [showExamples, setShowExamples] = useState(true)

  const [seed, setSeed] = useState<{ key: number; text: string } | null>(null)

  const effectiveProject =
    target === 'new' ? null : target === 'auto' ? (projects[0]?.id ?? null) : target

  /** 示例只把内容填进输入框，等用户改完再自己发送。 */
  const fillDraft = (example: HomeExample) => {
    tourBus.emit('research:action', { label: example.tag })
    setSeed({ key: Date.now(), text: example.text })
  }

  const run = async (text: string, title: string) => {
    const value = text.trim()
    if (!value || busy) return
    setBusy(true)
    try {
      tourBus.emit('research:action', { label: title })
      await onStart(value, title, effectiveProject, '')
      setSeed(null)
    } finally {
      setBusy(false)
    }
  }

  const examples = kind === 'research' ? RESEARCH_EXAMPLES : REVIEW_EXAMPLES

  return (
    <div className="research-home">
      <div className="research-center">
        <header className="rh-hero">
          <h1 className="rh-title">
            {kind === 'research' ? (
              <>
                今天想<span className="rh-accent">研究</span>点什么？
              </>
            ) : (
              <>
                今天想写点<span className="rh-accent">综述</span>？
              </>
            )}
          </h1>
        </header>

        <section className="research-composer">
          <div className="research-composer-top">
            <ProjectPicker
              projects={projects}
              value={target}
              disabled={busy}
              onChange={setTarget}
              onCreate={onCreateProject}
            />
          </div>
          <Composer
            kind={kind}
            variant="home"
            projectId={effectiveProject ?? undefined}
            running={busy}
            seed={seed ?? undefined}
            placeholder={
              kind === 'research'
                ? '描述你想研究的问题，/ 调用指令，@ 引用文件或论文'
                : '描述综述主题与目标读者，/ 调用指令，@ 引用文件或论文'
            }
            onSend={(text) => void run(text, text.slice(0, 40))}
            onStop={() => undefined}
            onOpenSettings={onOpenSettings}
          />
        </section>

        {showExamples && (
          <section
            className="rh-examples"
            data-tour={kind === 'research' ? 'research-actions' : 'review-actions'}
          >
            <header className="rh-examples-head">
              <span className="rh-examples-title">
                {kind === 'research' ? '研究任务示例' : '综述报告示例'}
              </span>
              <button
                type="button"
                className="rh-examples-close"
                title="关闭示例"
                aria-label="关闭示例"
                onClick={() => setShowExamples(false)}
              >
                <IconClose />
              </button>
            </header>
            <div className="rh-examples-list">
              {examples.map((example) => (
                <button
                  key={example.tag}
                  type="button"
                  className="rh-example"
                  onClick={() => fillDraft(example)}
                >
                  <span className="rh-example-tag">{example.tag}</span>
                  <span className="rh-example-text">{example.text}</span>
                </button>
              ))}
            </div>
          </section>
        )}
      </div>
    </div>
  )
}