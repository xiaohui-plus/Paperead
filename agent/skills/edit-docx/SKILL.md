---
name: edit-docx
description: 修改用户上传或导出的 Word 文档（.docx）：读取段落与表格，改文字/插入/删除段落，制作和编辑表格，设计标题与自定义样式，设置纸张/页边距/页眉页脚/页码，插入分页符、列表、图片、超链接和目录。用户说“改一下这个 Word / 修改 docx / 按格式排版 / 加个表格 / 设计标题”时使用。
whenToUse: 项目文件里有 .docx，用户要求改文字、加段落、改表格、调整字体字号缩进行距、设置标题样式、页面版式、页眉页脚、目录或插入图片时。
---

# 编辑 Word 文档

目标：在不破坏未涉及内容的前提下改对内容和版式，改完让用户能在应用里直接预览。

## 流程

1. `project_read` 找到目标 `.docx` 的路径；有多个 docx 且用户说“那个文档”时先问清楚。
2. `docx_read` 读取结构：
   - `paragraphs[]`：`index`、`style`、`text`；
   - `tables[]`：`index`、`rows`（二维文本）、`columns`、`style`；
   - `outline[]`：标题层级。
3. 在回复里说清楚要改哪里（引用段落/表格序号和原句）；用户没明确要求时不要大改结构。
4. `docx_edit` 传 `path` + `operations`（JSON 数组）。一次调用只做同一类改动，`index` 都以最近一次 `docx_read` 为准；同一次调用里插入/删除不会改变其他操作对原始段落序号的引用。
5. 改完再 `docx_read` 一次，确认内容和结构；排版参数以 `docx_edit` 返回的 `applied` 为准，再向用户汇报。
6. 用户要求“另存一份”时：复制文件不在本技能范围，说明会改原文件；需要保留原稿请用户先复制。

## 文字操作

- `{"op":"replace","find":"原文","replace":"新文"}`：全文替换，段落和表格单元格都会命中；
- `{"op":"set_paragraph","index":3,"text":"整段新文字"}`：重写整段，保留段落样式；
- `{"op":"insert_paragraph","after":3,"text":"新段落","style":"Heading 2"}`：插在该段之后；`after=-1` 插到文首；
- `{"op":"delete_paragraph","index":3}`；
- `{"op":"append_paragraph","text":"...","style":"Normal"}`：追加到文末。

## 段落排版

`{"op":"format_paragraph", ...}`，定位方式三选一：`index` 单段、`indices:[1,2]` 多段、`all:true` 全篇。

- 字体：`font`（中文名会同时写入东亚字体）、`fontSize`（磅）、`bold`、`italic`、`underline`、`color`（6 位十六进制，如 `000000`）；
- 段落：`align`（left/center/right/justify）、`firstLineIndentChars`（首行缩进字符数，中文常用 2）、`firstLineIndentPt`（首行缩进磅值，二选一）、`leftIndentPt`、`lineSpacing`（倍数，如 1.5）、`spaceBeforePt`、`spaceAfterPt`；
- 只传用户明确要求的属性，没传的属性保持原样。

## 标题与样式

- `{"op":"set_heading","index":0,"level":1}`：套用 Heading 1（没有内置样式时自动创建），并设置大纲级别，便于生成目录；
- `{"op":"create_style","name":"论文正文","base":"Normal","font":"宋体","fontSize":12,"align":"justify","firstLineIndentChars":2,"lineSpacing":1.5}`：新建段落样式；
- `{"op":"modify_style","style":"论文正文","fontSize":13}`：修改已有样式；
- `{"op":"set_style","indices":[1,2],"style":"论文正文"}`：把段落套到样式上；定位方式同样支持 `index` / `indices` / `all`。

## 表格

- 新建：`{"op":"insert_table","after":2,"rows":2,"columns":2,"data":[["指标","数值"],["准确率","85%"]],"header":true,"borders":"grid","widthsPt":[120,200],"align":"center"}`
  - `after` 省略表示文末；`header:true` 首行加粗、加底色并跨页重复；`borders` 支持 none/outer/all/grid；
- 行：`table_add_row`（`data`、`before` 可选）、`table_delete_row`；
- 列：`table_add_column`（`data`、`before` 可选）、`table_delete_column`；
- 合并：`{"op":"table_merge_cells","table":0,"startRow":0,"startColumn":0,"endRow":0,"endColumn":1}`；
- 改样式：`{"op":"table_set","table":0,"style":"Table Grid","header":true,"widthsPt":[...],"borders":"all","align":"center"}`；
- 单元格：`{"op":"set_table_cell","table":0,"row":1,"column":2,"text":"...","font":"宋体","fontSize":10.5,"bold":true,"align":"center"}`；
- 删除整表：`{"op":"delete_table","table":0}`；
- 表格序号按 `docx_read` 的 `tables[].index`；同一次调用里 `insert_table` 之后的 `table` 序号以插入后的顺序为准。

## 页面与页眉页脚

- 纸张：`{"op":"set_page","size":"A4","orientation":"landscape","marginTopCm":2.54,"marginBottomCm":2.54,"marginLeftCm":3.18,"marginRightCm":3.18}`；`size` 支持 A4/A3/A5/Letter/Legal，也可用 `widthCm`+`heightCm` 自定义；`section` 可选，默认全部节；
- 页眉/页脚：`{"op":"set_header","text":"综述草稿","pageNumber":true,"align":"right"}`，`set_footer` 同理；`pageNumber:true` 插入 PAGE 域（Word 打开后自动显示页码）；
- 分页：`{"op":"insert_page_break","after":3}`；
- 分节：`{"op":"insert_section_break","after":3,"type":"nextPage"}`，`type` 支持 nextPage / continuous。

## 列表、图片、链接、目录

- 列表：`{"op":"insert_list","after":3,"items":["第一点","第二点"],"ordered":true}`；文档没有内置列表样式时用「• / 1.」加悬挂缩进实现；
- 图片：`{"op":"insert_image","after":3,"image":"figures/fig1.png","widthCm":12,"align":"center","caption":"图 1 实验流程"}`；图片必须在当前项目目录内，`caption` 会另起一段居中；
- 超链接：`{"op":"insert_hyperlink","after":3,"text":"参考链接","url":"https://example.com"}`；
- 目录：`{"op":"insert_toc","levels":"1-3","after":-1}`，插入 TOC 域，需在 Word 里右键「更新域」生成实际目录。

## 约束

- 不要声称改过却没调用 `docx_edit`；工具报错时原样转述原因（样式不存在、序号越界、字号/颜色不合法等）。
- 只支持 `.docx`；遇到 `.doc` 让用户先在 Word 里另存为 `.docx`。
- 替换要精确：`find` 用文档里真实出现的一小段文字，不要用整段长句。
- 页眉页脚、页码、目录、图片、表格样式会影响整篇版式，改前先确认用户确实要改。
- 排版不确定时按常见中文学术格式：正文宋体小四（12pt）、1.5 倍行距、首行缩进 2 字符、两端对齐；标题用 Heading 1/2/3，保持字号逐级递减。
