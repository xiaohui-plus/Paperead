import { relative, resolve, sep } from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'research-tools'
export const inject = ['tools', 'shell', 'sandboxPolicy', 'approval']

const DEFAULT_BASE = 'http://127.0.0.1:8010'
const MAX_TEXT = 9000

// dsh 的提权阶梯：一次审批只允许向更宽的档位走一层。
const WIDER_MODES = {
  'read-only': ['workspace-write', 'danger-full-access'],
  'workspace-write': ['danger-full-access'],
}

function withinRoot(root, target) {
  const base = resolve(root)
  const next = resolve(target)
  if (next === base) return true
  return next.startsWith(base.endsWith(sep) ? base : base + sep)
}

function sandboxUnavailable(error) {
  return Boolean(error && typeof error === 'object' && error.code === 'SANDBOX_UNAVAILABLE')
}

// dsh 的 denial 签名只有英文；中文 Windows 上 PowerShell 会报「拒绝访问」，
// 这里补一层本地判定，避免沙箱已经拦住却当成普通命令失败、不向用户申请。
const DENIAL_HINTS = [
  'access is denied',
  'access to the path',
  'permission denied',
  '拒绝访问',
  '访问被拒绝',
  '未经授权',
  '权限不足',
]

function sandboxDenied(result) {
  if (result?.sandbox?.denied) return true
  const text = `${result?.stdout?.text || ''}\n${result?.stderr?.text || ''}`.toLowerCase()
  // 必须先看输出再看退出码：PowerShell 的非终止性错误（如 Set-Content 被拒）
  // 不会改变退出码，命令仍是 exit 0，只看 exitCode 会漏掉真实的沙箱拒绝。
  if (DENIAL_HINTS.some((hint) => text.includes(hint))) return true
  return false
}

function asText(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  return text.length > MAX_TEXT ? `${text.slice(0, MAX_TEXT)}\n…（已截断）` : text
}

function renderBlocks(data) {
  const authors = Array.isArray(data.authors) ? data.authors.join(', ') : data.authors || ''
  const head = `${data.title || data.paperId}${authors ? ` · ${authors}` : ''}`
  const body = (data.blocks || [])
    .map((block) => `【p${block.page}】${block.text}`)
    .join('\n\n')
  const tail = data.truncated ? '\n\n…（还有更多内容，可指定 page 继续读）' : ''
  return `${head}\n\n${body}${tail}`
}

function splitTags(value) {
  return String(value || '')
    .split(/[,，;；\n]/)
    .map((tag) => tag.trim())
    .filter(Boolean)
}

function sessionIdOf(exec) {
  const id = exec?.agent?.session?.id
  if (!id) throw new Error('缺少会话上下文：这个工具只能在研究项目的会话里调用。')
  return id
}

function textResult(title, content) {
  return {
    card: 'generic',
    title,
    content: typeof content === 'string' ? content : asText(content),
  }
}

export function apply(ctx, config = {}) {
  const apiBase = (config.apiBase || process.env.PAPEREAD_API_BASE || DEFAULT_BASE).replace(/\/+$/, '')
  const token = config.token || process.env.PAPEREAD_INTERNAL_TOKEN || ''

  async function call(path, params = {}, options = {}) {
    const url = new URL(apiBase + path)
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, String(value))
      }
    }
    const init = { headers: token ? { 'x-internal-token': token } : {} }
    if (options.method && options.method !== 'GET') {
      init.method = options.method
      init.headers['content-type'] = 'application/json'
      init.body = JSON.stringify(options.body ?? {})
    }
    const response = await fetch(url, init)
    const body = await response.text()
    if (!response.ok) {
      throw new Error(`Paperead API ${response.status}: ${body.slice(0, 300)}`)
    }
    return JSON.parse(body)
  }

  const shell = ctx.get('shell')
  const sandboxPolicy = ctx.get('sandboxPolicy')
  const approval = ctx.get('approval')

  /** 向用户申请一次授权；没有应答通道时按 dsh 的约定 fail-closed。 */
  async function askApproval(exec, reason) {
    if (!approval) return 'unavailable'
    try {
      return await approval.request({
        agent: exec.agent,
        toolName: 'run_command',
        callId: exec.callId,
        reason,
        signal: exec.signal,
      })
    } catch {
      return 'unavailable'
    }
  }

  ctx.tools.register(
    defineTool({
      name: 'library_search',
      description:
        'Search the user local paper library (titles, abstracts and parsed full text). Results are reranked by concept coverage, field hits and evidence quality, and each result carries the best page plus up to three matching pages, so you can cite a page without a separate lookup. The library is mostly English papers, so prefer short English keywords (e.g. "diffusion", "dual-target"); Chinese queries usually return nothing.',
      parameters: {
        query: {
          type: 'string',
          required: true,
          description: 'Keywords to search for.',
        },
        limit: {
          type: 'number',
          description: 'Max results, default 10, at most 30.',
        },
        mode: {
          type: 'string',
          description:
            'topic (default) searches title, abstract, tags and full text; title matches the title only, for verifying a known paper.',
        },
        minCoverage: {
          type: 'number',
          description:
            '0-1, default 0.5 for multi-concept queries. Lower it only when a query returns too few results.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'library_search',
          count: Array.isArray(value?.results) ? value.results.length : 0,
        }),
      },
      async execute(args, exec) {
        const sessionId = exec?.agent?.session?.id || ''
        return call('/internal/research/search/library', {
          q: args.query,
          limit: args.limit,
          mode: args.mode,
          minCoverage: args.minCoverage,
          sessionId,
        })
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'library_search',
          title: '检索本地文献库',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        const results = Array.isArray(value?.results) ? value.results : []
        const lines = results.map((item) => {
          const pages = Array.isArray(item.pages) && item.pages.length
            ? item.pages.map((page) => `p${page}`).join(', ')
            : item.page
              ? `p${item.page}`
              : '无页码'
          const meta = [item.year, item.venue].filter(Boolean).join(' · ')
          const coverage = typeof item.coverage === 'number'
            ? `覆盖 ${Math.round(item.coverage * 100)}%`
            : ''
          return `- ${item.title}${meta ? `（${meta}）` : ''} · ${pages} · ${coverage}\n  ${item.snippet}`
        })
        const summary = value?.notice || `命中 ${results.length} 篇。`
        return {
          card: 'generic',
          title: `本地文献 · ${results.length} 条`,
          content: `${summary}\n${lines.join('\n')}`.trim() || '没有找到相关段落。',
        }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'citation_check',
      description:
        'Audit a markdown review draft against the target review standard. Returns page-level citation ratio, unresolved paperIds, out-of-range pages, uncited claims, per-section source coverage, low-overlap citations, plus standard.gaps / standard.actions describing what is still missing (sections, evidence, citations) versus a full review. Run it after drafting, fetch whatever standard.actions asks for, revise, then run it again until standard.passed is true (max 3 rounds).',
      parameters: {
        source: {
          type: 'string',
          description:
            'Draft file name or artifact title. Omit to audit the most recent markdown draft.',
        },
        template: {
          type: 'string',
          description:
            'Review template id or uploaded template file name, e.g. "default", "narrative", "systematic", "file:我的模板.md", "upload:我的模板.md", or "paper:<paperId>" (use a library paper\'s section skeleton). Omit to use the project template; if none, the template chosen in app settings; if none, the system default applies.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'citation_check',
          pageRatio: value?.metrics?.pageRatio ?? null,
          issues: Array.isArray(value?.issues) ? value.issues.length : 0,
          standardScore: value?.standard?.score ?? null,
          standardPassed: value?.standard?.passed ?? null,
        }),
      },
      async execute(args, exec) {
        return call(
          `/internal/research/sessions/${encodeURIComponent(sessionIdOf(exec))}/citations/check`,
          { source: args.source, template: args.template },
        )
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'citation_check',
          title: '审计引用',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        const metrics = value?.metrics || {}
        const standard = value?.standard || {}
        const ratio = Math.round((metrics.pageRatio || 0) * 100)
        const head =
          `页码级引用 ${ratio}% · 来源 ${metrics.sourcesUnique ?? 0} 个 · ` +
          `全文 ${metrics.fulltextSources ?? 0} 篇 · 未解析 ${metrics.unresolved ?? 0} · ` +
          `无引用论断 ${metrics.uncitedClaims ?? 0}`
        const standardLine =
          standard.score == null
            ? ''
            : `标准差距 ${standard.score}%${standard.passed ? '（已达标）' : '（未达标）'}`
        const gaps = (standard.gaps || []).slice(0, 8).map((gap) => `- ${gap}`)
        const actions = (standard.actions || []).slice(0, 6).map((item) => `- 下一步：${item}`)
        const issues = (value?.issues || [])
          .slice(0, 8)
          .map((issue) => `- [${issue.kind}] 第 ${issue.line} 行：${issue.detail}`)
        const suggestions = (value?.suggestions || []).map((item) => `- ${item}`)
        const title = standardLine
          ? `引用审计 · ${ratio}% · 标准 ${standard.score}%`
          : `引用审计 · ${ratio}%`
        return textResult(
          title,
          [head, standardLine, ...gaps, ...actions, ...issues, ...suggestions]
            .filter(Boolean)
            .join('\n'),
        )
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'run_command',
      description:
        'Run one shell command inside the project working folder (PowerShell on Windows) under the current sandbox permission preset. Use it for compiling LaTeX, running python/R analysis, git status and file conversion. In workspace-write mode only the project folder is writable; a file denial is reported as "[sandbox: ...]" — that is a policy decision, not a broken command. When a denied command genuinely needs wider access, retry the exact same command once with sandbox_permissions plus a one-sentence justification; the approval prompt raised by that retry is how the user consents. In danger-full-access mode the command runs unconfined. Do not install system software, modify permissions or run destructive commands.',
      parameters: {
        command: {
          type: 'string',
          required: true,
          description: 'Command to execute, e.g. "python analysis.py".',
        },
        cwd: {
          type: 'string',
          description: 'Relative working directory inside the project working folder. Defaults to the folder root.',
        },
        timeoutMs: {
          type: 'number',
          description: 'Timeout in milliseconds, default 120000, max 600000.',
        },
        sandbox_permissions: {
          type: 'string',
          enum: ['workspace-write', 'danger-full-access'],
          description:
            'Wider sandbox mode this command needs. Only valid as a one-shot retry of a command the sandbox just denied; requires justification and user approval.',
        },
        justification: {
          type: 'string',
          description:
            'Required with sandbox_permissions: one sentence for the user explaining why this exact command needs wider access.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [
          {
            type: 'text',
            text: [value?.stdout, value?.stderr].filter(Boolean).join('\n').trim() || '（无输出）',
          },
        ],
        presentationMeta: (_args, value) => ({
          kind: 'terminal',
          exitCode: value?.exitCode ?? null,
        }),
      },
      async execute(args, exec) {
        const session = exec.agent?.session
        const root = session?.header?.cwd
        if (!session || !root) throw new Error('缺少会话上下文：这个工具只能在研究项目的会话里调用。')

        const text = String(args.command || '').trim()
        if (!text) throw new Error('命令不能为空。')

        let policy = sandboxPolicy.resolve({ session })
        if (args.sandbox_permissions) {
          const target = String(args.sandbox_permissions)
          const justification = String(args.justification || '').trim()
          if (!justification) throw new Error('使用 sandbox_permissions 时必须给出 justification。')
          const wider = WIDER_MODES[policy.mode] || []
          if (!wider.includes(target)) {
            throw new Error(`不能从当前 ${policy.mode} 沙箱直接提权到 ${target}。`)
          }
          const outcome = await askApproval(
            exec,
            `将沙箱从 ${policy.mode} 提升到 ${target}：${justification}`,
          )
          if (outcome !== 'allowed-once') throw new Error('用户没有批准这次提权，命令未执行。')
          policy = { ...policy, mode: target }
        }

        const workdir = args.cwd ? resolve(root, args.cwd) : root
        if (policy.mode !== 'danger-full-access' && !withinRoot(root, workdir)) {
          throw new Error('工作目录必须位于项目工作文件夹内。')
        }

        const started = Date.now()
        const runOnce = (nextPolicy) =>
          shell.run(
            shell.resolve({
              command: text,
              workdir,
              ...(args.timeoutMs ? { timeoutMs: args.timeoutMs } : {}),
              sandboxPolicy: nextPolicy,
              ...(exec.signal ? { signal: exec.signal } : {}),
            }),
          )

        let result
        try {
          result = await runOnce(policy)
        } catch (error) {
          if (!sandboxUnavailable(error) || policy.mode === 'danger-full-access') throw error
          const outcome = await askApproval(
            exec,
            '本机沙箱无法启动，是否以完全权限执行这条命令？',
          )
          if (outcome !== 'allowed-once') {
            throw new Error('沙箱不可用，用户没有批准以完全权限执行。')
          }
          policy = { ...policy, mode: 'danger-full-access' }
          result = await runOnce(policy)
        }

        if (result.aborted) throw new Error('命令已取消。')

        if (sandboxDenied(result) && policy.mode !== 'danger-full-access') {
          const mode = result.sandbox?.mode ?? policy.mode
          const outcome = await askApproval(
            exec,
            `命令被 ${mode} 沙箱拒绝，是否以完全权限重新执行？`,
          )
          if (outcome === 'allowed-once') {
            policy = { ...policy, mode: 'danger-full-access' }
            result = await runOnce(policy)
          }
        }

        return {
          command: text,
          cwd: workdir === root ? '.' : relative(root, workdir),
          exitCode: result.exitCode,
          stdout: result.stdout?.text ?? '',
          stderr: result.stderr?.text ?? '',
          truncated: Boolean(result.stdout?.truncated || result.stderr?.truncated),
          timedOut: Boolean(result.timedOut),
          durationMs: Date.now() - started,
          sandbox: result.sandbox
            ? {
                mode: result.sandbox.mode,
                denied: Boolean(result.sandbox.denied),
                enforcement: result.sandbox.enforcement,
              }
            : null,
        }
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'terminal',
          title: '运行命令',
          rawInput: { command: args.command, cwd: args.cwd },
        }
      },
      presentResult(_args, value) {
        if (value?.timedOut) {
          return textResult('命令超时', `超过时限被终止。\n${value?.stderr || ''}`)
        }
        const code = value?.exitCode ?? '?'
        const body = [value?.stdout, value?.stderr].filter(Boolean).join('\n').trim()
        return textResult(`终端 · 退出码 ${code}`, body || '（无输出）')
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'paper_read',
      description:
        'Read parsed text of one library paper. Pass page to read a single page; otherwise reads from the start up to maxChars.',
      parameters: {
        paperId: {
          type: 'string',
          required: true,
          description: 'Paper id returned by library_search.',
        },
        page: {
          type: 'number',
          description: 'Only read this page.',
        },
        maxChars: {
          type: 'number',
          description: 'Max characters to return, default 6000.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(renderBlocks(value)) }],
        presentationMeta: (_args, value) => ({
          kind: 'paper_read',
          paperId: value?.paperId ?? null,
          pages: (value?.blocks || []).length,
        }),
      },
      async execute(args) {
        try {
          return await call(
            `/internal/research/papers/${encodeURIComponent(args.paperId)}/read`,
            { page: args.page, maxChars: args.maxChars },
          )
        } catch (error) {
          if (/API 409/.test(String(error?.message || ''))) {
            return {
              paperId: args.paperId,
              error: '这篇论文还在解析中，暂时读不了；请稍后再试，不要重复导入。',
              needsIngest: true,
            }
          }
          throw error
        }
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'paper_read',
          title: args.page ? `精读第 ${args.page} 页` : '读取论文',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        if (value?.error) return textResult('暂时读不了', value.error)
        return {
          card: 'generic',
          title: value?.title || '论文内容',
          content: asText(renderBlocks(value || {})),
        }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'academic_search',
      description:
        'Search external scholarly databases (arXiv, PubMed, OpenAlex, Europe PMC, Semantic Scholar, CORE) and return a batch of results WITH ABSTRACTS for screening. Defaults to abstractOnly=true, so records without abstracts are filtered out. Use 20-30 results per batch, judge relevance from the abstracts, and only use paper_import when the user explicitly asks to read full text.',
      parameters: {
        query: {
          type: 'string',
          required: true,
          description: 'Search query; Chinese queries are translated automatically.',
        },
        source: {
          type: 'string',
          description:
            'arxiv | pubmed | openalex | europepmc | semanticscholar | core. Default openalex (all disciplines). core requires a configured API key.',
        },
        yearFrom: { type: 'number', description: 'Earliest publication year.' },
        yearTo: { type: 'number', description: 'Latest publication year.' },
        limit: { type: 'number', description: 'Max results, default 10, at most 30. Use 20-30 for batch screening.' },
        abstractOnly: {
          type: 'boolean',
          description: 'Default true: only return records that have an abstract. Set false only if nothing comes back.',
        },
        abstractMaxChars: {
          type: 'number',
          description: 'Max abstract characters per paper, default 700, at most 2000.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'academic_search',
          count: Array.isArray(value?.papers) ? value.papers.length : 0,
          source: value?.source ?? null,
        }),
      },
      async execute(args) {
        return call('/internal/research/scholar/search', {
          q: args.query,
          source: args.source,
          yearFrom: args.yearFrom,
          yearTo: args.yearTo,
          limit: args.limit,
          abstractOnly: args.abstractOnly,
          abstractMaxChars: args.abstractMaxChars,
        })
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'academic_search',
          title: `学术检索 · ${args.source || 'openalex'}`,
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        if (value?.error) return textResult('学术检索失败', value.error)
        const papers = Array.isArray(value?.papers) ? value.papers : []
        const lines = papers.map((paper) => {
          const meta = [paper.year, paper.venue, paper.source].filter(Boolean).join(' · ')
          const flag = paper.hasAbstract ? '摘要' : '仅题录'
          return `- ${paper.title}${meta ? `（${meta}）` : ''} · ${flag}\n  id: ${paper.id}`
        })
        const summary =
          value?.notice ||
          `返回 ${papers.length} 篇，其中 ${value?.withAbstract ?? papers.length} 篇带摘要。`
        return textResult(`学术检索 · ${papers.length} 篇`, `${summary}\n${lines.join('\n')}`.trim() || '没有结果。')
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'paper_import',
      description:
        'Download the open-access PDF of one academic_search result into the current project paper folder (NOT the global library) and parse it, so it can be read with paper_read. Pass the paper id exactly as returned. Prefer using abstracts from academic_search first; only import when full text is needed.',
      parameters: {
        paperId: {
          type: 'string',
          required: true,
          description: 'Paper id from academic_search, e.g. arxiv:2401.00001.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'paper_import',
          imported: Boolean(value?.imported),
          already: Boolean(value?.already),
        }),
      },
      async execute(args, exec) {
        const sessionId = sessionIdOf(exec)
        return call(
          `/internal/research/sessions/${encodeURIComponent(sessionId)}/papers/import`,
          {},
          { method: 'POST', body: { id: args.paperId } },
        )
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'paper_import',
          title: '导入项目论文',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        if (value?.error) return textResult('导入失败', value.error)
        const paper = value?.paper || {}
        const verb = value?.already ? '已在项目论文里' : value?.imported ? '已导入项目' : '未导入'
        return textResult(
          `${verb} · ${paper.title || ''}`,
          [
            paper.title || '',
            `paperId: ${paper.id || ''}`,
            value?.item?.path ? `项目文件: ${value.item.path}` : '',
          ]
            .filter(Boolean)
            .join('\n'),
        )
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'project_read',
      description:
        'Read the current research project context: name, kind, research question, instructions, project files, artifacts and recent sessions. Call it before planning or writing so you use the real project instead of guessing.',
      parameters: {},
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'project_read',
          files: Array.isArray(value?.files) ? value.files.length : 0,
          artifacts: Array.isArray(value?.artifacts) ? value.artifacts.length : 0,
        }),
      },
      async execute(_args, exec) {
        const sessionId = sessionIdOf(exec)
        return call(`/internal/research/sessions/${encodeURIComponent(sessionId)}/project`)
      },
      presentCall() {
        return { card: 'generic', kind: 'project_read', title: '读取项目上下文' }
      },
      presentResult(_args, value) {
        const project = value?.project || {}
        const files = (value?.files || []).map((file) => `- ${file.path || file.name}`)
        return textResult(
          `项目 · ${project.name || ''}`,
          [
            `类型：${project.kind || ''}`,
            `研究问题：${project.researchQuestion || '（未填写）'}`,
            `文件：${files.length ? `\n${files.join('\n')}` : '（空）'}`,
            `产物：${(value?.artifacts || []).length} 个`,
          ].join('\n'),
        )
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'project_write',
      description:
        'Write to the current research project. Use it to update the project profile (description / researchQuestion / instructions) and to persist deliverables: target=artifact writes into the project artifacts (reports, decks, notes), target=file writes into the project file tree. Always persist long deliverables instead of only replying in chat.',
      parameters: {
        content: {
          type: 'string',
          description:
            'Markdown, plain text, or LaTeX source (.tex) to save. Omit when only updating the profile.',
        },
        title: { type: 'string', description: 'Human title of the saved file/artifact.' },
        filename: {
          type: 'string',
          description: 'Optional file name, e.g. review-draft.md or main.tex.',
        },
        target: {
          type: 'string',
          description: 'artifact (default) or file.',
        },
        name: { type: 'string', description: 'New project name, if renaming.' },
        description: { type: 'string', description: 'New project description.' },
        researchQuestion: { type: 'string', description: 'New main research question.' },
        instructions: { type: 'string', description: 'Standing instructions for this project.' },
        reviewTemplate: {
          type: 'string',
          description:
            'Writing template for this project: "default" (通用综述五段式), a builtin id ("paper-empirical" for IMRaD research papers, "narrative" / "systematic" for reviews), an uploaded template file name in the project files (e.g. "我的5A模板.md"), an app-level upload ("upload:我的模板.md"), or a library paper ("paper:<paperId>"). Leave empty to fall back to app settings.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'project_write',
          artifact: value?.artifact?.title ?? null,
          file: value?.file?.path ?? null,
          updatedProfile: Boolean(value?.project),
        }),
      },
      async execute(args, exec) {
        const sessionId = sessionIdOf(exec)
        const base = `/internal/research/sessions/${encodeURIComponent(sessionId)}`
        const result = {}
        const patch = {}
        for (const key of ['name', 'description', 'researchQuestion', 'instructions', 'reviewTemplate']) {
          if (typeof args[key] === 'string' && args[key].trim()) patch[key] = args[key]
        }
        if (Object.keys(patch).length) {
          const saved = await call(`${base}/project`, {}, { method: 'PATCH', body: patch })
          result.project = saved.project
        }
        if (typeof args.content === 'string' && args.content.trim()) {
          const body = {
            content: args.content,
            title: args.title,
            filename: args.filename,
          }
          if (args.target === 'file') {
            const saved = await call(`${base}/files`, {}, { method: 'POST', body })
            result.file = saved.file
          } else {
            const saved = await call(`${base}/artifacts`, {}, { method: 'POST', body })
            result.artifact = saved.artifact
          }
        }
        if (!result.project && !result.file && !result.artifact) {
          throw new Error('没有可写入的内容：请提供 content 或至少一个项目字段。')
        }
        return result
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'project_write',
          title: args.target === 'file' ? '写入项目文件' : '保存项目产物',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        const parts = []
        if (value?.project) parts.push(`项目：${value.project.name}`)
        if (value?.file) parts.push(`文件：${value.file.path}`)
        if (value?.artifact) parts.push(`产物：${value.artifact.title}`)
        return textResult('项目已更新', parts.join('\n') || '已写入。')
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'file_read',
      description:
        'Read a text file saved in the current project (Markdown, txt, json, csv, bib, tex). Use project_read first to get the exact path or file name. Use this to read a draft back before revising it; artifact_read only covers artifacts, not project files.',
      parameters: {
        path: {
          type: 'string',
          required: true,
          description: 'Relative path or file name shown by project_read.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'file_read',
          name: value?.file?.name || '',
        }),
      },
      async execute(args, exec) {
        const sessionId = sessionIdOf(exec)
        return call(
          `/internal/research/sessions/${encodeURIComponent(sessionId)}/files`,
          { path: args.path },
        )
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'file_read',
          title: '读取文件',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        return textResult(
          value?.file?.name || '文件内容',
          value?.content || '（二进制文件，无法直接预览）',
        )
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'docx_read',
      description:
        'Read a Word (.docx) file in the current project. Returns paragraphs with stable index, style and text, every table as rows of cells, and the heading outline. Use project_read first to get the exact path, then use the paragraph index values with docx_edit. Old .doc files are not supported: ask the user to save them as .docx first.',
      parameters: {
        path: {
          type: 'string',
          required: true,
          description: 'Relative path or file name shown by project_read, e.g. 综述草稿.docx.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'docx_read',
          paragraphs: Array.isArray(value?.paragraphs) ? value.paragraphs.length : 0,
          tables: Array.isArray(value?.tables) ? value.tables.length : 0,
        }),
      },
      async execute(args, exec) {
        return call(
          `/internal/research/sessions/${encodeURIComponent(sessionIdOf(exec))}/docx`,
          { path: args.path },
        )
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'docx_read',
          title: `读取 Word · ${args.path}`,
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        const paragraphs = Array.isArray(value?.paragraphs) ? value.paragraphs : []
        const tables = Array.isArray(value?.tables) ? value.tables : []
        const lines = paragraphs
          .slice(0, 60)
          .map((item) => `[${item.index}] ${item.style || 'Normal'}｜${item.text}`)
        if (paragraphs.length > 60) lines.push(`…（还有 ${paragraphs.length - 60} 段）`)
        const tableLines = tables.map(
          (table, index) =>
            `表格 ${index}：${table.rows.length} 行 × ${table.rows[0]?.length || 0} 列`,
        )
        return textResult(
          `Word · ${paragraphs.length} 段 · ${tables.length} 个表格`,
          [...lines, ...tableLines].join('\n') || '文档里没有可读内容。',
        )
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'docx_edit',
      description:
        'Edit a Word (.docx) in place while keeping its existing styles. Call docx_read first and pass paragraph/table index values from that result; inside one call every paragraph index still refers to the original paragraph list even if you also insert or delete. operations is a JSON array, each item is one of: ' +
        'TEXT: {"op":"replace","find":"旧文字","replace":"新文字"}; {"op":"set_paragraph","index":3,"text":"整段新文字"}; {"op":"insert_paragraph","after":3,"text":"新段落","style":"Heading 2"}; {"op":"delete_paragraph","index":3}; {"op":"append_paragraph","text":"...","style":"Normal"}; ' +
        'FORMAT: {"op":"format_paragraph","index":3,"font":"宋体","fontSize":12,"align":"justify","firstLineIndentChars":2,"lineSpacing":1.5} (target by index, indices:[1,2] or all:true; also bold, italic, underline, color, firstLineIndentPt, leftIndentPt, spaceBeforePt, spaceAfterPt); ' +
        'STYLE: {"op":"create_style","name":"论文正文","base":"Normal","font":"宋体","fontSize":12,"firstLineIndentChars":2,"lineSpacing":1.5}; {"op":"modify_style","style":"论文正文",...}; {"op":"set_style","indices":[1,2],"style":"论文正文"}; {"op":"set_heading","index":0,"level":1}; ' +
        'TABLE: {"op":"insert_table","after":2,"rows":2,"columns":2,"data":[["指标","数值"],["准确率","85%"]],"header":true,"borders":"grid","widthsPt":[120,200],"align":"center"} (borders: none/outer/all/grid); {"op":"table_add_row","table":0,"data":[...],"before":1}; {"op":"table_delete_row","table":0,"row":2}; {"op":"table_add_column","table":0,"data":[...]}; {"op":"table_delete_column","table":0,"column":1}; {"op":"table_merge_cells","table":0,"startRow":0,"startColumn":0,"endRow":0,"endColumn":1}; {"op":"table_set","table":0,"style":"...","header":true,"widthsPt":[...],"borders":"all","align":"center"}; {"op":"delete_table","table":0}; {"op":"set_table_cell","table":0,"row":1,"column":2,"text":"...", "font":"宋体","fontSize":10.5,"bold":true,"align":"center"}; ' +
        'PAGE: {"op":"set_page","size":"A4","orientation":"landscape","marginTopCm":2.54,"marginBottomCm":2.54,"marginLeftCm":3.18,"marginRightCm":3.18} (size: A4/A3/A5/Letter/Legal or widthCm+heightCm; section index optional); {"op":"set_header","text":"...","pageNumber":true,"align":"right"}; {"op":"set_footer",...}; {"op":"insert_page_break","after":3}; {"op":"insert_section_break","after":3,"type":"nextPage"}; ' +
        'CONTENT: {"op":"insert_list","after":3,"items":["a","b"],"ordered":true}; {"op":"insert_image","after":3,"image":"figures/fig1.png","widthCm":12,"align":"center","caption":"图 1 ..."}; {"op":"insert_hyperlink","after":3,"text":"链接","url":"https://..."}; {"op":"insert_toc","levels":"1-3","after":-1}. ' +
        'Prefer several small operations over rewriting the whole document, and call docx_read again afterwards to verify. Never claim the file was changed without calling this tool. Old .doc files are not supported.',
      parameters: {
        path: {
          type: 'string',
          required: true,
          description: 'Relative path or file name of the .docx to edit.',
        },
        operations: {
          type: 'string',
          required: true,
          description:
            'JSON array of operations, e.g. [{"op":"replace","find":"待补充","replace":"已补充"},{"op":"append_paragraph","text":"新的总结段。"}]',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'docx_edit',
          applied: Array.isArray(value?.applied) ? value.applied.length : 0,
          paragraphs: value?.paragraphs ?? null,
        }),
      },
      async execute(args, exec) {
        let operations
        if (typeof args.operations === 'string' && args.operations.trim()) {
          try {
            operations = JSON.parse(args.operations)
          } catch {
            throw new Error(
              'operations 必须是 JSON 数组，例如 [{"op":"replace","find":"旧","replace":"新"}]',
            )
          }
        } else if (Array.isArray(args.operations)) {
          operations = args.operations
        } else {
          throw new Error('operations 必须是 JSON 数组。')
        }
        return call(
          `/internal/research/sessions/${encodeURIComponent(sessionIdOf(exec))}/docx`,
          {},
          { method: 'POST', body: { path: args.path, operations } },
        )
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'docx_edit',
          title: `修改 Word · ${args.path}`,
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        const applied = Array.isArray(value?.applied) ? value.applied : []
        const summary = applied
          .map((item) => {
            const bits = [item.op]
            if (item.index != null) bits.push(`#${item.index}`)
            if (Array.isArray(item.indices)) bits.push(`#${item.indices.join(',')}`)
            if (Array.isArray(item.fields)) bits.push(`（${item.fields.join('、')}）`)
            if (item.occurrences != null) bits.push(`（命中 ${item.occurrences} 处）`)
            for (const key of ['table', 'row', 'column', 'image', 'style', 'level', 'items', 'name']) {
              if (item[key] != null) bits.push(`${key}=${item[key]}`)
            }
            return `- ${bits.join(' ')}`
          })
          .join('\n')
        return textResult(
          `Word 已修改 · ${applied.length} 个操作`,
          `${summary || '已保存。'}\n当前：${value?.paragraphs ?? '?'} 段 · ${value?.tables ?? '?'} 个表格`,
        )
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'artifact_read',
      description:
        'List the artifacts of the current project (reports, decks, notes), or read one artifact by id when artifactId is given.',
      parameters: {
        artifactId: {
          type: 'string',
          description: 'Artifact id. Omit to list all artifacts of the project.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'artifact_read',
          count: Array.isArray(value?.artifacts) ? value.artifacts.length : value?.artifact ? 1 : 0,
        }),
      },
      async execute(args, exec) {
        if (args.artifactId) {
          return call(`/internal/research/artifacts/${encodeURIComponent(args.artifactId)}`)
        }
        const sessionId = sessionIdOf(exec)
        return call(`/internal/research/sessions/${encodeURIComponent(sessionId)}/artifacts`)
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'artifact_read',
          title: args.artifactId ? '读取产物' : '列出产物',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        if (value?.artifact) {
          return textResult(value.artifact.title, value.content || '（二进制产物，无法直接预览）')
        }
        const items = (value?.artifacts || []).map(
          (item) => `- ${item.title}（${item.kind}）${item.id}`,
        )
        return textResult('项目产物', items.join('\n') || '还没有产物。')
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'library_tag',
      description:
        'List local library papers with their tags, or tag one paper. Pass paperId + tags to tag; omit both to list papers so you can deduplicate and cluster them.',
      parameters: {
        paperId: { type: 'string', description: 'Paper id to tag. Omit to list papers.' },
        tags: {
          type: 'string',
          description: 'Comma-separated tags, e.g. "免疫治疗,耐药机制".',
        },
        mode: {
          type: 'string',
          description: 'append (default) keeps existing tags; replace overwrites them.',
        },
        limit: { type: 'number', description: 'Max papers when listing, default 50.' },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'library_tag',
          count: Array.isArray(value?.papers) ? value.papers.length : 0,
          paperId: value?.paperId ?? null,
        }),
      },
      async execute(args) {
        const tags = splitTags(args.tags)
        if (args.paperId && tags.length) {
          return call(`/internal/research/papers/${encodeURIComponent(args.paperId)}/tags`, {}, {
            method: 'PUT',
            body: { tags, mode: args.mode || 'append' },
          })
        }
        return call('/internal/research/library/papers', { limit: args.limit })
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'library_tag',
          title: args.paperId ? '给文献打标签' : '列出文献库',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        if (value?.paperId) {
          return textResult('标签已更新', `${value.paperId}：${(value.tags || []).join('、')}`)
        }
        const papers = (value?.papers || []).map((paper) => {
          const tags = (paper.tags || []).join('、') || '未打标签'
          return `- ${paper.title}（${tags}）`
        })
        return textResult(`文献库 · ${papers.length} 篇`, papers.join('\n') || '文献库为空。')
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'export_document',
      description:
        'Export a project document: DOCX (pandoc), BibTeX, LaTeX (.tex), PDF (compile .tex with the local TeX engine) or PPTX (16:9 slide deck). Use format=latex to turn a markdown draft into a compilable .tex (Chinese defaults to ctexart), format=pdf to compile an existing .tex or a markdown draft into PDF, and format=pptx with deck to build a real .pptx for group meetings. Always writes real files into the project tree; never claim a file exists without this tool.',
      parameters: {
        format: {
          type: 'string',
          description: 'docx (default) | bib | latex | pdf | pptx.',
        },
        source: {
          type: 'string',
          description:
            'Project file or artifact name to convert (e.g. review-draft.md or main.tex); omit to use the newest markdown document.',
        },
        content: {
          type: 'string',
          description:
            'Inline markdown (or a complete .tex starting with \\documentclass) to convert, instead of source.',
        },
        title: {
          type: 'string',
          description: 'Title used for the template and output filename.',
        },
        filename: {
          type: 'string',
          description: 'Output filename, e.g. review.docx, main.tex or references.bib.',
        },
        author: {
          type: 'string',
          description: 'Author line for the LaTeX template.',
        },
        abstract: {
          type: 'string',
          description: 'Optional abstract text for the LaTeX template.',
        },
        template: {
          type: 'string',
          description:
            'LaTeX template: auto (default, Chinese draft -> ctexart, otherwise article), ctexart or article.',
        },
        compile: {
          type: 'boolean',
          description:
            'For latex: also compile a PDF when a local TeX engine exists (default true).',
        },
        papers: {
          type: 'string',
          description:
            'For bib: comma-separated paperIds from library_search / academic_search.',
        },
        subtitle: {
          type: 'string',
          description: 'For pptx: subtitle on the title slide.',
        },
        deck: {
          type: 'string',
          description:
            'For pptx: a JSON array of slides. Each slide: {"kind":"title|section|bullets","title":"结论式标题","subtitle":"","bullets":["要点"],"chart":"柱状图：...","source":"Li 2026, p3","notes":"讲稿"}. Omit to convert a markdown outline automatically.',
        },
        pptTemplate: {
          type: 'string',
          description:
            'For pptx: optional filename of a .pptx template in the current project. Only pass it when the user explicitly asks for a project template; when omitted, the deck uses the PPT template selected in Settings → Templates → PPT 模板.',
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: asText(value) }],
        presentationMeta: (_args, value) => ({
          kind: 'export_document',
          format: value?.format ?? null,
          files: Array.isArray(value?.files) ? value.files.length : 0,
        }),
      },
      async execute(args, exec) {
        const sessionId = sessionIdOf(exec)
        let deck
        if (typeof args.deck === 'string' && args.deck.trim()) {
          try {
            deck = JSON.parse(args.deck)
          } catch {
            throw new Error('deck 必须是 JSON 数组，例如 [{"kind":"bullets","title":"...","bullets":["..."]}]')
          }
        } else if (Array.isArray(args.deck)) {
          deck = args.deck
        }
        const result = await call(
          `/internal/research/sessions/${encodeURIComponent(sessionId)}/export`,
          {},
          {
            method: 'POST',
            body: {
              format: args.format || 'docx',
              source: args.source,
              content: args.content,
              title: args.title,
              subtitle: args.subtitle,
              filename: args.filename,
              author: args.author,
              abstract: args.abstract,
              template: args.template,
              compile: args.compile,
              papers: splitTags(args.papers),
              deck,
              pptTemplate: args.pptTemplate,
            },
          },
        )
        if (result?.error) throw new Error(result.error)
        return result
      },
      presentCall(args) {
        const format = args.format || 'docx'
        const labels = {
          bib: '导出 BibTeX',
          latex: '导出 LaTeX',
          tex: '导出 LaTeX',
          pdf: '编译 PDF',
          pptx: '制作 PPT',
          ppt: '制作 PPT',
        }
        return {
          card: 'generic',
          kind: 'export_document',
          title: labels[format] || '导出 DOCX',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        if (value?.error) return textResult('导出失败', value.error)
        const files = (value?.files || []).map(
          (file) => `- ${file.name}（${file.size} 字节）\n  路径: ${file.path}`,
        )
        const notes = []
        if (value?.compiled) notes.push(`已编译 PDF（${value.engine || 'LaTeX'}）`)
        if (value?.warning) notes.push(`未编译 PDF：${value.warning}`)
        if (typeof value?.slides === 'number') notes.push(`共 ${value.slides} 页`)
        if (value?.template && value.template !== 'builtin') {
          notes.push(`已套用模板：${value.template}`)
        }
        const audit = value?.citation
        if (audit?.metrics) {
          const metrics = audit.metrics
          notes.push(
            `引用审计：页码级 ${Math.round((metrics.pageRatio || 0) * 100)}%` +
              ` · 未解析 ${metrics.unresolved || 0}` +
              ` · 无引用论断 ${metrics.uncitedClaims || 0}`,
          )
          for (const warning of audit.warnings || []) notes.push(`注意：${warning}`)
          if (audit.report) notes.push(`审计报告：${audit.report}`)
        }
        return textResult(
          `已导出 · ${value?.format || ''}`,
          [files.join('\n') || '没有生成文件。', ...notes].join('\n\n'),
        )
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'echo',
      description:
        'Echo the given text back verbatim. Used by the Paperead research host spike to prove the tool pipeline and card projection.',
      parameters: {
        text: {
          type: 'string',
          required: true,
          description: 'Text to echo back.',
        },
      },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
        // Durable presentation payload: survives replay through tool/result.meta.
        presentationMeta: (_args, value) => ({ kind: 'echo', text: value }),
      },
      async execute(args) {
        return args.text
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'echo',
          title: '回声',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        return {
          card: 'generic',
          title: '回声完成',
          content: value,
        }
      },
    }),
  )
}
