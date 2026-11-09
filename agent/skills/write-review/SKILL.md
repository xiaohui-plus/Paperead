---
name: write-review
description: 写综述：列大纲、按节找证据、逐节起草、核验引用并导出 docx/bib/LaTeX。用户说“写综述 / 写初稿 / 列大纲 / 核引用 / 转格式 / 用 LaTeX 写”时使用。
whenToUse: 需要产出综述、相关工作或研究背景章节时。
---

# 写综述

目标：每个论断都能追溯到真实文献，查不到证据的地方显式留空。

## 流程

1. `project_read`：读取综述主题、已有大纲与产物（如果已有草稿，先 `artifact_read` 读它）。
2. 列大纲：4-7 个一级章节，每节写清要回答的问题、所需证据类型、预期图表。
3. 按小节找证据：
   - 先 `library_search` + `paper_read` 用本地全文；
   - 本地不足再 `academic_search`（一次取 20-30 条，按摘要筛选）；默认不下载 PDF，确需核对方法细节时最多 `paper_import` 1-2 篇。
4. 逐节起草。引用格式：`（作者 年份, paperId, p页码）`；外部文献用 `（作者 年份, DOI 或 id）`。若按 `write-latex` 交付 LaTeX，正文引用改用 `\citep{key}`，key 必须与 `references.bib` 一致。
5. 核引用：逐条确认文献存在、且该页/该段确实支持该句；不支持的删除或降级为「相关但未直接证明」。
6. 用 `project_write` 保存草稿（建议 `target=artifact`，文件名 `review-draft.md`），并在回复里给出产物标题与章节结构。
7. 转格式：用户要 docx / bib / LaTeX / PDF 时调用 `export_document`：
   - `export_document(format="docx", source="review-draft.md")` 把草稿转成真正的 `.docx` 写进项目文件树；
   - `export_document(format="bib", papers="paperId1,paperId2,...")` 生成 BibTeX；
   - 用户明确要 LaTeX 时，按 `write-latex` 技能写完整 `main.tex`，再用 `export_document(format="latex" | "pdf", source="main.tex")` 生成 `.tex` / 编译 PDF；
   - 回复里给出真实文件路径与包含的章节，不要声称写了二进制文件却没调用工具。

## 约束

- **绝不编造参考文献、页码或数据**。没有证据就写【待补证据】。
- 不要大段转述单篇论文；按主题组织，多篇互证。
- 区分「一致结论」「存在争议」「证据不足」三种状态。
- 如果用户只要某一节，就只写那一节，但引用仍要完整。
