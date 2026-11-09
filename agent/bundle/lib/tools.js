import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'research-tools'
export const inject = ['tools']

const DEFAULT_BASE = 'http://127.0.0.1:8010'
const MAX_TEXT = 9000

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

  ctx.tools.register(
    defineTool({
      name: 'library_search',
      description:
        'Search the user local paper library (titles, abstracts and parsed full text). Each result carries a ~700-char abstract excerpt plus a text snippet; judge relevance from the abstract and do NOT call paper_read just to understand the topic. The library is mostly English papers, so prefer short English keywords (e.g. "diffusion", "dual-target"); Chinese queries usually return nothing.',
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
          const page = item.page ? ` p${item.page}` : ''
          return `- ${item.title}${page}：${item.snippet}`
        })
        return {
          card: 'generic',
          title: `本地文献 · ${results.length} 条`,
          content: lines.length ? lines.join('\n') : '没有找到相关段落。',
        }
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
        for (const key of ['name', 'description', 'researchQuestion', 'instructions']) {
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
        'Export a project document: DOCX (pandoc), BibTeX, LaTeX (.tex) or PDF (compile .tex with the local TeX engine). Use format=latex to turn a markdown draft into a compilable .tex (Chinese defaults to ctexart), and format=pdf to compile an existing .tex or a markdown draft into PDF. Always writes real files into the project tree; never claim a file exists without this tool.',
      parameters: {
        format: {
          type: 'string',
          description: 'docx (default) | bib | latex | pdf.',
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
              filename: args.filename,
              author: args.author,
              abstract: args.abstract,
              template: args.template,
              compile: args.compile,
              papers: splitTags(args.papers),
            },
          },
        )
        if (result?.error) throw new Error(result.error)
        return result
      },
      presentCall(args) {
        const format = args.format || 'docx'
        const labels = { bib: '导出 BibTeX', latex: '导出 LaTeX', tex: '导出 LaTeX', pdf: '编译 PDF' }
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
