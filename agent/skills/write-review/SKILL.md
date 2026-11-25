---
name: write-review
description: 写综述或研究论文：列大纲、按节建证据卡、逐节起草、引用审计并导出 docx/bib/LaTeX；也可对待评审稿件按 FATAL/MAJOR/MINOR 出具审稿意见。用户说“写综述 / 写论文 / 写初稿 / 列大纲 / 核引用 / 转格式 / 用 LaTeX 写 / 审稿 / 批评这篇论文”时使用。
whenToUse: 需要产出综述、研究论文草稿、相关工作或研究背景章节，或需要对他人稿件/草稿给出结构化评审意见时。
---

# 写综述

目标：每个论断都能追溯到真实文献与真实页码，查不到证据的地方显式留空。

## 流程

先判断模板类型（`reviewTemplateKind`）：
- `paper`（如 `paper-empirical`、`paper-draft`）：按下面的「论文写作」分支执行，产出论文草稿，不是综述。
- `review` 或为空：按下面的「综述写作」分支执行。

1. `project_read`：读取写作主题、`reviewTemplate`、`reviewTemplateKind`、`reviewTemplateOutline`、`reviewTemplateMarkdown`、已有大纲与产物。若已有草稿，用 `file_read` 读回正文；`artifact_read` 只覆盖 artifacts，不要拿它读项目文件。
2. **定模板（四级优先）**：用户本次指定 > 项目 `reviewTemplate` > 应用设置里的「论文模板」> `files/` 里名字含“模板/template”的 `.md` > 系统默认（通用综述）。
   - 内置模板：`paper-empirical`（实证研究论文，IMRaD）、`paper-draft`（论文初稿写作模板，问题导向论证与语言规范）、`narrative`（叙述性综述）、`systematic`（系统综述 PRISMA 2020）、`default`（通用综述五段式）。
   - `project_read` 返回的 `reviewTemplateSource` 说明当前骨架来自 `project` / `global` / `default`；`global` 可能是设置页上传的模板（`upload:xxx.md`）或文献库综述（`paper:<paperId>`）。
   - `reviewTemplateOutline` 是每节的“篇幅 / 必写 / 自检”约束，`reviewTemplateMarkdown` 是完整模板正文；写作时逐节对照，不要只看章节标题。
   - 用户要换模板：`project_write(reviewTemplate="paper-empirical")`、`project_write(reviewTemplate="systematic")` 或 `reviewTemplate="我的5A模板.md"`（模板文件需已在项目文件里）；只想用一次就 `citation_check(template=...)`。
   - 用户要从文献库选一篇综述当模板：优先提示用户在「设置 → 论文模板」里选择；若只是本次使用，`citation_check(template="paper:<paperId>")` 会按该文献的目录骨架检查。骨架为空或提示提取失败时，先 `paper_read` 它的目录/前两页，把章节标题提取成 `project_write(target="file", filename="review-template.md")`，再按该骨架写作。
   - 用户上传了模板但没指定：先 `project_read` 看文件名，选最新的模板文件并告知用户。
   - **模板的图表与语言也要学**：`project_read` 返回 `reviewTemplateGuide` 时，除章节骨架外还给出该论文的图表放置与语言风格——按 `sectionStats` / `figures` / `tables` 决定每章的图表数量与位置（如模板在「方法」章配 2 图 1 表，本稿也应在此章安排图表），按 `style.citationStyle` 选引用格式（numeric 用 `[n]`，author-year 用「作者 年份」），按 `style.avgSentenceLength`、`connectives`、`hedges` 控制句长与措辞。没有数据支撑的图不要硬造，改用表、示意图或文字说明。
   - **说清图表怎么来的**：`figures` / `tables` 每项都带 `type`（时间线图、架构示意图、方法对比表等）、`purpose`（为什么放在这一章）与 `context`（正文如何引用它）。写正文时据此决定图表形态，并在图注或首次引用处用一句话交代“基于什么数据/文献、回答什么问题”；不能只放一张无来源说明的图。
   - **论文格式也要学**：`reviewTemplateGuide.format` 给出页面与栏数、正文字号、页边距、一级标题编号体系（罗马/阿拉伯/字母/中文/不编号）、图表题分隔符、是否有摘要与关键词、参考文献节标题。Markdown 稿按标题编号与图表题格式写；导出 LaTeX 时按 `columns` 与 `marginsMm` 设置版式，标题层级与模板一致。
3. 列大纲：4-7 个一级章节，每节写清“要回答的问题 + 所需证据类型 + 预期图表 + 对应模板约束”。项目文件里有 `review-template.md`（或 `综述模板.md`）时，**按模板的二级标题当骨架**，缺哪节补哪节；没有模板时按 `default` 的「摘要 / 引言 / 方法 / 主题章节 / 讨论 / 结论」组织。每个章节都要对照 `reviewTemplateOutline` 里的 `points` 与 `checks`，不要只写标题。模板带 `reviewTemplateGuide` 时，把它的图表分布直接写进各节的“预期图表”。
4. **先建证据卡，再写正文**：
   - 每节用 2-3 组不同英文关键词调用 `library_search`，优先选 `coverage` 高、`missing` 少、`pages` 非空的结果；
   - 按结果里的 `pages`（最多 3 个候选页）调用 `paper_read(paperId, page=...)`，摘录不超过 40 词的原文；
   - 每节整理 2-4 张卡：`{paperId, page, quote, 支持什么论点}`，用 `project_write` 存成 `evidence-notes.md`（target=file）；
   - 本地全文不足时再用 `academic_search` 补摘要级证据，最多 4 次，每次 20-30 条。
5. 逐节起草：**只从证据卡扩写**，不要凭记忆补内容。本地引用写 `（作者 年份, paperId, p页码）`；外部摘要来源写 `（作者 年份, DOI）` 并注明“依据摘要”，**不得给它编页码**。LaTeX 交付时改用 `\citep{key}`，key 必须与 `references.bib` 一致。
6. **差距自检 → 回补 → 复查（最多 3 轮；论文模板下只做引用审计，不套综述五段式门槛）**：
   - 调用 `citation_check(source="review-draft.md")`，先读 `standard.score`、`standard.gaps`、`standard.actions`，再看 `issues`；
   - `standard.passed=false` 时按 `actions` 逐条回补，不要只改措辞：
     - 缺章节 → 补写该节（先结论，再证据）；
     - 某主题小节来源 < 2 → 换 2-3 组英文关键词 `library_search`，按 `pages` 调 `paper_read` 摘原文，先补证据卡再改正文；
     - 本地全文 < 5 篇 → `academic_search` 取 20-30 条，挑最相关的 `paper_import`，导入后 `paper_read`；
     - 页码级引用率 < 60% → 对关键论断补 `paper_read(page)` 并标页码；
     - 未解析引用 / 无引用论断 → 删除、替换，或降级为【待补证据】；
   - 每轮回补后重跑 `citation_check`；`standard.passed=true`，或连续两轮 `score` 不再上升时停止；
   - 收尾时把仍未解决的 `standard.gaps` 原样告诉用户，不得声称“已达标”。
7. **终稿润色（推荐）**：按 `polish-writing` 技能逐段处理正文，只改措辞，不改数据、引用标号与结论；润色后重跑 `citation_check`，确认引用数量与页码未变。
8. 保存草稿：`project_write`（建议 `target=artifact`，文件名 `review-draft.md`），回复里给出产物标题、章节结构、审计指标与标准得分。
9. 转格式（仅在用户要求时）：`export_document(format="docx", source="review-draft.md")`、`format="bib"`、或按 `write-latex` 写 `main.tex` 后 `format="latex" | "pdf"`。回复里给真实文件路径，不要声称写了二进制文件却没调用工具。导出的 docx 会自动套用中文论文版式（正文小四仿宋、1.2 倍行距、两端对齐且末行不拉伸、首行缩进 2 字符；一级/二级/三级标题分别为三号黑体、小三黑体、四号宋体加粗；西文用 Times New Roman），正文里不需要再写排版说明；LaTeX 中文稿（ctexart）同样套用这套版式，英文稿保持 article 默认排版。


## 论文写作（reviewTemplateKind = paper）

选择 `paper-empirical`、`paper-draft` 或模板类型为 `paper` 时，按论文写，不要套用综述的“证据面/主题小节”门槛：

1. 按 `reviewTemplateOutline` 的章节顺序写：`paper-empirical` 为「标题与作者 → 摘要 → 关键词 → 引言 → 相关工作 → 方法 → 实验与结果 → 讨论 → 结论 → 附录」；`paper-draft` 为「标题 → 摘要 → 引言 → 研究方法 → 实验设置与评估方案 → 结果与讨论 → 结论 → 数据、代码与声明」。两者都以 outline 为准，不要混用。
2. 选 `paper-draft` 时先按其 `reviewTemplateMarkdown` 建立论证关系表（研究问题 / 现有不足及文献依据 / 本文设计回应 / 待检验假设 / 检验方式 / 证据状态），再逐节起草；语言和段落按模板第九至十一章（正式直接、一个句子一个主要判断、区分观察/解释/推测、图表与引用规范）。
3. 摘要按「研究任务及意义 → 关键不足 → 本文方法 → 核心设计 → 主要结果 → 受证据支持的意义」写；结果必须有真实数值（均值、比例、效应量、95% CI 或 p 值），数字与正文表格一致。
4. 方法、实验数据只能来自：项目文件（数据表、代码输出、实验记录）、用户明确提供的内容、或 `paper_read` 读到的真实文献。**没有实验数据就写【待补实验数据】，绝不编造数值、基线或消融结果。**
5. 相关工作与引言引用仍按真实 `paperId`/DOI 标注；没有本地全文就注明“依据摘要”，不得编页码。
6. 产物保存为 `paper-draft.md`，回复中列出：章节是否齐全、缺失的数据/实验、还需要用户补充的材料。

## 评审他人稿件

用户给的是待评审的论文/草稿，或明确要“审稿意见、批评、修改计划”时，走评审，不要当成要写一篇综述：

1. 先读全稿（`file_read` / `paper_read`），列出稿件自己的核心主张与证据链，再开始批评；没读到的部分标「未读」，不要假设。
2. 问题按严重度分级，每条都要给位置和修改方向：
   - `FATAL`：结论不成立、数据/引用造假、方法无法支撑主张、伦理或统计错误。存在 FATAL 时结论只能是「大修」或「拒稿」，不能写「建议小修」。
   - `MAJOR`：缺关键基线/消融、样本或指标不匹配、相关工作遗漏导致新颖性存疑。
   - `MINOR`：表述、图表、引用格式、可读性。
3. 对稿件的量化主张做抽查核对：能对上的写「核对一致（来源）」，对不上或查不到写「未核实」，绝不默认作者是对的。
4. 输出顺序：总体判断（接受/小修/大修/拒稿）→ 分级问题清单 → 必须补的实验或证据 → 逐节修改建议。批评要具体到句子或图表编号，不要写“建议加强论证”这类空话。
5. 评审意见与核查记录一起 `project_write` 存成产物，并在末尾附「核查记录」：核对了哪些引用/数字、结果如何、哪些无法访问。

## 约束

- **绝不编造参考文献、页码或数据**。没有证据就写【待补证据】。
- 只有对同一个 `paperId` 调用过 `paper_read(page)`，才能写该页页码；`library_search` 的 `pages` 只是候选。
- 本地全文来源少于 5 篇时，明确告诉用户“证据面偏窄，建议先导入并精读 N 篇”，不要用摘要撑篇幅。
- 不要大段转述单篇论文；按主题组织，多篇互证。
- 区分「一致结论」「存在争议」「证据不足」三种状态。
- 状态词只用：`已核对`、`未核实`、`有争议`、`检索不到`、`未执行`。没有实际调用工具核对过的内容，不能写成“已验证”。
- 交付综述或评审时附一条「来源记录」：用了哪些 paperId/DOI、多少条来自本地全文、多少条仅摘要、哪些断言仍未核实；写到同一个产物文件末尾，不单独造文件。
- 如果用户只要某一节，就只写那一节，但引用仍要完整。
