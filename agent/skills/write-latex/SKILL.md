---
name: write-latex
description: 用 LaTeX 写综述或论文：套用内置中文/英文模板、生成 .tex 与 references.bib、编译 PDF。用户明确说“用 LaTeX / 写 tex / 要 PDF 投稿稿”时使用。
whenToUse: 用户要求以 LaTeX 交付综述、论文或章节时。
---

# 用 LaTeX 写作

目标：交付可直接编译的 LaTeX 工程，而不是只给一段 LaTeX 片段。

## 触发

- 用户明确提到 LaTeX、tex、XeLaTeX/pdfLaTeX、投稿模板、`.tex` 或“要 PDF 稿”。
- 用户没有要求时仍默认 Markdown，不主动改成 LaTeX。

## 流程

1. `project_read` + `artifact_read` 读已有大纲与草稿；已有 `review-draft.md` 时先读它。
2. 定模板：
   - 中文稿：`ctexart`（默认，XeLaTeX 编译）；
   - 英文稿：`article`；
   - 用户给了期刊或学校模板：按模板的 documentclass 与字段来，不要覆盖。
3. 写文件（`project_write`，`target=file`）：
   - 主文件 `main.tex`，必须包含完整 `\documentclass`、导言区、`\begin{document}...\end{document}`；
   - 不要把整篇 Markdown 塞进 tex；用 `\section`、`\subsection` 组织；
   - 图用项目文件树里已有的图片，`\includegraphics[width=0.9\linewidth]{文件名}`（相对 `main.tex` 所在目录）；不要引用不存在的图片；
   - 表格用 booktabs（`\toprule / \midrule / \bottomrule`）；
   - 公式用 `$...$` 或 `equation / align`。
4. 引用：
   - 正文只用 `\cite{key}` / `\citep{key}`，key 必须来自真实文献；
   - `references.bib` 用 `export_document(format="bib", papers="paperId1,paperId2")` 生成；
   - 没有对应 key 就写 `% TODO: 补引用`，不要编造。
5. 编译：
   - 已有 `main.tex`：`export_document(format="pdf", source="main.tex")`；
   - 只有 Markdown 初稿：`export_document(format="latex", source="review-draft.md", title="...")`，会自动生成 `.tex` 并尽量编译 PDF；
   - 编译失败时读错误里的行号，修 `main.tex` 后再编译，最多 3 轮；仍然失败就交付 `.tex` 并说明原因。
6. 交付：列出 `.tex` / `.bib` / `.pdf` 的真实路径、章节结构、未解决的引用与 TODO。

## 内置模板

中文稿（ctexart）的最小可用骨架，可直接作为 `main.tex` 的起点：

```latex
\documentclass[UTF8,a4paper,11pt]{ctexart}
\usepackage[margin=2.5cm]{geometry}
\usepackage{amsmath,amssymb,amsfonts}
\usepackage{graphicx}
\usepackage{booktabs,longtable,array,multirow}
\usepackage{natbib}
\title{题目}
\author{作者}
\date{\today}
\begin{document}
\maketitle
\begin{abstract}
摘要。
\end{abstract}
\section{引言}
正文，引用写作 \citep{key}。
\bibliographystyle{plainnat}
\bibliography{references}
\end{document}
```

英文稿把文档类换成 `article`，去掉 `UTF8` 选项即可。

## 规范

- 中文稿用 XeLaTeX；英文稿优先 XeLaTeX，兼容 pdfLaTeX。
- 标题、作者只在模板里写一次；正文不要再写一级标题（导出时会去重）。
- 特殊字符转义：`& % $ # _ { } ~ ^`；数学公式内不要转义。
- 不要使用 `\write18`、`--shell-escape`、在线安装宏包等不可控操作。
- 不声称“已编译”除非 `export_document` 返回 `compiled=true`；没有 TeX 引擎时明确说明。

## 质量检查

- [ ] `.tex` 有完整 documentclass 环境，能被 XeLaTeX 编译
- [ ] 每个 `\cite` 都能在 `.bib` 找到
- [ ] 图表有标题和交叉引用
- [ ] 公式与正文术语前后一致
