# 文献检索（替换「参考文献引用助手」）· 设计

日期：2026-11-08
状态：已确认
参考项目：`F:\Project\Scolar_search\scholar-search-hub`（TanStack Start + React）

## 1. 目标

在 paper-reflow 里提供「检索外部文献 → 看详情 → 一键导入文献库 → 重排阅读 + 翻译」的闭环，
替换侧栏里一直禁用的「参考文献引用助手」占位。

成功标准：

1. 侧栏点「文献检索」即可搜索 arXiv / PubMed / OpenAlex / Europe PMC，中文查询自动译英。
2. 详情页能把开放获取 PDF 下载进文献库并后台解析，随即可进入阅读页翻译。
3. 能复制 GB/T 7714、APA、MLA、IEEE、BibTeX、RIS 六种引用，能导出当前页 BibTeX/RIS。
4. 无关键词时展示每日推荐，可「换一批」，同一天结果稳定。
5. 所有外部调用失败都有中文提示与重试，不出现空白页。

## 2. 关键取舍

| 取舍 | 决定 | 理由 |
|---|---|---|
| 后端语言 | 用 Python 重写检索逻辑 | 本项目是 FastAPI + Tauri sidecar，加 Node 运行时会让安装包多一层依赖 |
| 检索与文献库 | 打通：下载 PDF → `LibraryStore` → 现有 `_parse_task` | 重排+翻译是本项目独有价值，只跳外链等于丢掉差异 |
| 引用与导出 | 在 Python 侧实现 | 与 pytest 共用一套逻辑，前端不做第二份格式化代码 |
| 路由 | 沿用 `?v=search&...` 查询参数 | 与现有 `?p=` 极简路由一致，不引入 router 库 |
| 每日推荐分类 | 沿用参考项目的 AI / 生物医学两个池 | 已验证的池；后续可配置 |

## 3. 目录与职责

```
paper_reflow/scholar/
  __init__.py
  models.py    Paper / Searcher 输入输出类型；文本清洗：decode_xml、strip_latex_pointers、
               clean_html_text、reconstruct_abstract、strip_doi、epmc_key/split_epmc_key
  http.py      TTL + LRU 缓存、同 key 在途合并、超时/重试/退避、FriendlyError、
               arXiv 3 秒节流闸门（asyncio.Lock + 单调时钟）
  sources.py   四源查询构造与结果解析：
               build_arxiv_query / parse_arxiv_entries（Atom）
               parse_openalex_works（含倒排摘要还原）
               parse_europe_pmc（fullTextUrlList 取 OA PDF）
               pubmed：esearch → esummary → efetch 解析
  search.py    search(input) → SearchResult；get_paper(id)；find_free_full_text(doi)；
               to_english_query(q)（Google 两端点兜底）
  daily.py     heat_score / rank_daily / daily_batch / to_date_key / mulberry32 等价实现
  cite.py      format_citation(paper, style) / build_bibtex / build_ris / 六种样式
server/
  scholar_api.py   build_router(store, add_pdf, public) → APIRouter，由 main.py 注入文献库能力
web/src/
  lib/math.ts            含 LaTeX 文本切分（移植参考项目，含裸 LaTeX 启发式）
  lib/scholar.ts         API 客户端与类型
  components/MathText.tsx
  components/ScholarPage.tsx       页面壳 + 状态 + URL 同步
  components/ScholarSearchBar.tsx  来源切换 / 搜索框 / 高级筛选
  components/ScholarResult.tsx     结果条目（标题高亮、摘要悬停展开）
  components/ScholarDaily.tsx      每日推荐
  components/ScholarDetail.tsx     详情 + 导入文献库 + 复制引用 + 全文
web/src/App.tsx       增加 `v=search` 视图与「从检索页打开阅读页」的返回目标
web/src/components/Sidebar.tsx  导航替换为 文献库 / 文献检索 / 研究 / 写综述报告
web/src/styles.css    新增 .scholar-* 样式，复用既有 CSS 变量与 library 布局
```

## 4. 后端接口

| 方法 | 路径 | 入参 | 出参 |
|---|---|---|---|
| GET | `/api/scholar/search` | `q, source, sort, page, field, phrase, category, from_year, to_year, per_page` | `{papers, total, pageLimit, usedQuery, translated, error}` |
| GET | `/api/scholar/daily` | `round` | `{papers, date, poolSize, error}` |
| GET | `/api/scholar/paper` | `id` | `Paper`；404 表示查不到 |
| GET | `/api/scholar/fulltext` | `doi` | `{pdfUrl, landingUrl, oaStatus, error}` |
| POST | `/api/scholar/import` | `{id}` | `{paper, imported, already}` |
| POST | `/api/scholar/cite` | `{paper, style}` | `{text}` |
| POST | `/api/scholar/export` | `{papers, format}` | `{text, filename}` |

`Paper` 字段与参考项目一致：`id(source:rawId) / source / title / authors / venue / year /
category / keywords / abstract / url / pdfUrl / htmlUrl / publishedAt / doi / citations`。

### 数据源细节

- **arXiv**：`export.arxiv.org/api/query`，`search_query` 用 `all/ti/abs/au` 前缀、短语加引号、
  `cat:` 与 `submittedDate` 区间；`sortBy` 映射 rel/new/updated；每 3 秒最多一次外呼。
- **PubMed**：esearch（`retmode=json`）→ esummary → efetch（摘要分节、关键词、MeSH）。
- **OpenAlex**：`works` + `select` 精简字段 + `mailto` 礼貌池；摘要由倒排索引按位置还原。
- **Europe PMC**：cursor 窗口 200 条，用「一次多取、本地切片」实现翻页，并回传 `pageLimit`。
- **Unpaywall**：`/v2/{doi}?email=`，404 视为「无 OA 记录」而非故障。

### 缓存与限流

检索 5 分钟、详情 6 小时、译文 24 小时、Unpaywall 6 小时、arXiv HTML 探测 1 小时。
缓存命中或在途合并时不进入 arXiv 节流窗口。失败不写缓存、不留 inflight。

### 中文查询

含 CJK 时先请求 Google 免密钥端点（`clients5` 主、`gtx` 兜底），译文为空或失败则退回原查询，
响应里带 `translated` 与 `usedQuery`，前端回显实际检索词并据此做高亮。

## 5. 导入文献库

`POST /api/scholar/import`：

1. `get_paper(id)` 取元信息；`pdfUrl` 为空且有 DOI 时查 Unpaywall。
2. 下载前校验：仅 `http/https`、非 `localhost`/内网地址、超时 60s、上限 60MB。
3. 下载后校验 `%PDF` 魔数（或 `application/pdf`），否则报「该链接不是有效 PDF」。
4. 去重：`store.find_by_source(source_id)`，命中直接返回已有 id。
5. `store.add(filename, data, meta={source, source_id, title, authors, doi, url})`，
   然后 `asyncio.create_task(_parse_task(id))`，响应返回文献库行（含 `file_url`/`thumb_url`）。
6. 前端拿到 id 后跳阅读页；解析未完成时阅读页已有「正在解析」自动重试逻辑。

`LibraryStore` 增量：`add(..., meta: dict | None)`、`find_by_source(source_id)`。

## 6. 前端行为

- **导航**：`文献库`（默认）/`文献检索`/`研究`（禁用）/`写综述报告`（禁用）。
- **检索页**：
  - 来源标签、搜索框（回车提交，输入停顿 450ms 也提交）、高级检索折叠（字段、短语、分类、年份、每页）。
  - 无关键词 → 每日推荐（标题「每日推荐」+ 日期 + 分类名 + 换一批）。
  - 有关键词 → 结果数、排序、列表、分页、导出。
  - 摘要默认两行，悬停 0.8s 原地展开。
- **详情**：元信息表、摘要（KaTeX）、主按钮「导入文献库并阅读」、下载 PDF / 查找免费全文 /
  复制引用（下拉六种）/ 原文页。
- **URL 即状态**：`?v=search&q=&src=&sort=&page=&field=&phrase=&cat=&from=&to=&per=&pid=`。
  阅读页仍是 `?p=`；从检索页进入的阅读页返回时回检索页。

## 7. 错误与降级

| 场景 | 行为 |
|---|---|
| 超时 | 「检索请求超时，请检查网络后重试。」不自动重试 |
| 429 | 「请求过于频繁……请稍后重试。」指数退避重试 2 次 |
| 5xx / 网络错误 | 退避重试后仍失败给中文提示 |
| 某推荐池失败 | 其余池照常合并，`poolSize` 变小 |
| 无 OA 全文 | 按钮变「查找免费全文」或「原文页」 |
| 后端未启动 | 检索页显示「本地服务未启动」提示 |

## 8. 测试

- `tests/test_scholar_sources.py`：arXiv 查询语法、Atom 解析、OpenAlex 倒排摘要、Europe PMC key/OA PDF、
  LaTeX 指针宏剔除、HTML 摘要清洗、PubMed 摘要分节。
- `tests/test_scholar_cite.py`：六种格式与 BibTeX/RIS 关键片段。
- `tests/test_scholar_daily.py`：热度单调性、同日稳定、换批不重叠、分类均衡。
- `tests/test_scholar_http.py`：TTL 命中、并发合并只外呼一次、失败不进缓存、重试次数。
- `tests/test_scholar_routes.py`：search 路由 mock 后返回结构；import 用假 PDF 走通并去重。
- 前端以 `tsc && vite build` 把关。

## 9. 不做

知网/校园 VPN 入口、内嵌 arXiv HTML 阅读、检索历史与收藏夹、跨源合并去重、机构订阅全文。
