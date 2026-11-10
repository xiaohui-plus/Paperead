import type { TourPayload } from './tourBus'
import { isModelReady, loadModelSettings } from '../lib/settings'

export type TourPlacement = 'right' | 'bottom' | 'left' | 'top'

export interface TourStep {
  id: string
  chapter: string
  title: string
  body: string
  action: string
  /** 空字符串表示居中完成卡，不需要锚点。 */
  selector: string
  route?: string
  prepare?: 'open-first-paper' | 'open-latest-project' | 'open-review-project' | 'open-latest-review-project'
  event?: string | string[]
  match?: (payload: TourPayload) => boolean
  /** 进入这一步时已经满足条件，不再等用户重复操作。 */
  ready?: () => boolean
  requiredTabs?: string[]
  placement?: TourPlacement
  optional?: boolean
}

function textOf(payload: TourPayload): string {
  return `${payload.text ?? ''} ${payload.label ?? ''}`.trim()
}

export const tourSteps: TourStep[] = [
  {
    id: 'config-model',
    chapter: '准备',
    title: '先把模型接上',
    body: '后面的导读、问答、综述都靠它。点侧栏底部「模型与密钥」，选一个服务商、填入 Key，保存后会自动回到这里。',
    action: '点击「模型与密钥」，完成配置并保存',
    selector: '[data-tour="settings-entry"]',
    route: '/',
    event: 'settings:saved',
    ready: () =>
      loadModelSettings().models.some(
        (model) => model.kind === 'answer' && isModelReady(model),
      ),
    placement: 'right',
  },
  {
    id: 'library-upload',
    chapter: '文献库',
    title: '放进第一篇论文',
    body: '支持 PDF，多选也行。上传后会自动解析版面、图表和公式，并打开阅读页。',
    action: '点击「添加文章」，或把 PDF 拖进窗口',
    selector: '[data-tour="library-upload"]',
    route: '/',
    event: 'library:uploaded',
    ready: () => Boolean(document.querySelector('.paper-card:not(.add-card)')),
    placement: 'bottom',
  },
  {
    id: 'library-zotero',
    chapter: '文献库',
    title: '已有的文献，和 Zotero 接上',
    body: '点「Zotero 同步」，粘贴一个带写权限的 API Key 就完成连接：远端有、本地没有的条目连同 PDF 拉进来；本地新增的论文和分类推到远端，两边互相合并。',
    action: '点击「Zotero 同步」，填入 API Key 并保存',
    selector: '[data-tour="zotero-sync"]',
    route: '/',
    placement: 'bottom',
  },
  {
    id: 'reader-mode',
    chapter: '读一篇',
    title: '对照读，不用来回翻',
    body: '切到「对照」，左边原文、右边译文；译文会随滚动逐段出现，不会一次等整篇。',
    action: '点击顶部「对照」',
    selector: '[data-tour="reader-mode"]',
    prepare: 'open-first-paper',
    event: 'reader:mode-changed',
    placement: 'bottom',
  },
  {
    id: 'reader-guide',
    chapter: '读一篇',
    title: '先看导读，再决定怎么读',
    body: '点「导读」生成一句话概括、术语表和阅读顺序；每篇都会说明为什么读、读完能得到什么。',
    action: '点击左侧「导读」，等待生成完成',
    selector: '[data-tour="reader-tab-guide"]',
    prepare: 'open-first-paper',
    event: 'reader:guide-ready',
    ready: () => Boolean(document.querySelector('[data-tour-guide="ready"]')),
    placement: 'right',
  },
  {
    id: 'reader-chat',
    chapter: '读一篇',
    title: '问一句，答案落回原文',
    body: '在「问答」里问核心贡献或局限，回答里的引用可以跳回对应段落，不必只读摘要。',
    action: '切换到「问答」，发送一个问题',
    selector: '[data-tour="reader-tab-chat"]',
    prepare: 'open-first-paper',
    event: 'reader:chat-answered',
    ready: () => Boolean(document.querySelector('[data-tour-chat="ready"]')),
    placement: 'right',
  },
  {
    id: 'reader-panels',
    chapter: '读一篇',
    title: '目录、重点、图表都在左栏',
    body: '用目录跳章节，用重点看结论，用图表看实验；「脉络」会梳理这篇论文的来龙去脉。',
    action: '依次打开「目录」「重点」「图表」',
    selector: '[data-tour="reader-tabs"]',
    prepare: 'open-first-paper',
    event: 'reader:tab-visited',
    requiredTabs: ['outline', 'key', 'figures'],
    placement: 'right',
  },
  {
    id: 'reader-comments',
    chapter: '读一篇',
    title: '划词高亮，顺手写批注',
    body: '在正文里选中一句话，四色高亮任选；高亮和批注都会收进左栏「批注」，可以按页回看、跳回原文，导出 Markdown 时一起带走。',
    action: '打开左侧「批注」看看',
    selector: '[data-tour="reader-tab-comments"]',
    prepare: 'open-first-paper',
    event: 'reader:tab-visited',
    requiredTabs: ['comments'],
    placement: 'right',
  },
  {
    id: 'reader-attachments',
    chapter: '读一篇',
    title: '补充材料放「附件」',
    body: '每篇论文可以单独上传补充材料、数据集或实验记录，随时预览；它们只跟着这篇论文，不会混进项目文件。',
    action: '打开左侧「附件」',
    selector: '[data-tour="reader-tab-files"]',
    prepare: 'open-first-paper',
    event: 'reader:tab-visited',
    requiredTabs: ['files'],
    placement: 'right',
  },
  {
    id: 'search-paper',
    chapter: '找文献',
    title: '缺什么，就去检索',
    body: '在文献检索里搜关键词，先看摘要再决定要不要读全文；需要时把结果加入文献库。',
    action: '搜索一个关键词，打开一篇结果',
    selector: '[data-tour="search-input"]',
    route: '?v=search',
    event: 'search:result-opened',
    placement: 'bottom',
  },
  {
    id: 'research-home',
    chapter: '做研究',
    title: '从读一篇，到做一个方向',
    body: '研究页把文献、会话和产物放在同一个项目里，随时可以回来继续。',
    action: '点击侧栏「研究」',
    selector: '[data-tour="nav-research"]',
    route: '/',
    event: 'research:opened',
    placement: 'right',
  },
  {
    id: 'research-action',
    chapter: '做研究',
    title: '让 agent 先跑一遍找文献',
    body: '点一个快捷动作，agent 会创建项目与会话，把过程和结果都留在项目里。',
    action: '点击「找文献」',
    selector: '[data-tour="research-actions"]',
    route: '?view=research',
    event: 'research:action',
    placement: 'bottom',
  },
  {
    id: 'project-structure',
    chapter: '做研究',
    title: '文件、会话、产物都在这里',
    body: '「会话」是研究过程，「文件」是上传的资料，「产物」是 agent 写出的结果。',
    action: '分别点一次「文件」和「会话」',
    selector: '[data-tour="project-rail-tabs"]',
    prepare: 'open-latest-project',
    event: 'project:tab-changed',
    requiredTabs: ['files', 'sessions'],
    placement: 'right',
  },
  {
    id: 'composer-tools',
    chapter: '做研究',
    title: '把资料直接递给 agent',
    body: '输入框左下角可以上传附件、切换模型；模型选择只影响后续消息。',
    action: '上传一个附件，或切换一次模型',
    selector: '[data-tour="composer-tools"]',
    prepare: 'open-latest-project',
    event: ['composer:attachment-added', 'composer:model-changed'],
    placement: 'top',
  },
  {
    id: 'review-outline',
    chapter: '写综述',
    title: '写综述，从大纲开始',
    body: '先列章节和每节要回答的问题，再逐节找证据，避免一上来就写。',
    action: '点击「列大纲」',
    selector: '[data-tour="review-actions"]',
    route: '?view=review',
    event: ['research:action', 'composer:message-sent'],
    match: (payload) => textOf(payload).includes('大纲'),
    placement: 'bottom',
  },
  {
    id: 'review-draft',
    chapter: '写综述',
    title: '逐节起草，引用可追溯',
    body: '让 agent 按大纲写初稿；每个论断都要能追溯到文献和页码，查不到证据的地方会被标出来。',
    action: '在输入框发送「帮我写综述初稿」',
    selector: '[data-tour="composer"]',
    prepare: 'open-review-project',
    event: ['composer:message-sent', 'research:action'],
    match: (payload) => textOf(payload).includes('初稿'),
    placement: 'top',
  },
  {
    id: 'review-export',
    chapter: '写综述',
    title: '导出前，先让引用过一遍',
    body: '发送导出指令，生成 docx / bib 文件；导出前会自动生成引用审计报告。',
    action: '发送「帮我转格式，导出 docx 和 bib」',
    selector: '[data-tour="composer"]',
    prepare: 'open-latest-review-project',
    event: ['composer:message-sent', 'research:action'],
    match: (payload) => {
      const text = textOf(payload)
      return text.includes('转格式') || text.includes('导出')
    },
    placement: 'top',
  },
  {
    id: 'done',
    chapter: '完成',
    title: '你已完成第一遍',
    body: '读一篇、找一次文献、做一个研究项目、写一篇综述。之后可以从设置里重新打开教程。',
    action: '',
    selector: '',
  },
]
