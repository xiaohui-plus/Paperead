# 文献检索 Implementation Plan

> **For agentic workers:** 按 task 顺序执行；每个 task 先写测试再实现，跑通后再进入下一个。
> 设计见 `docs/superpowers/specs/2026-11-08-scholar-search-design.md`。

**Goal:** 把参考项目的多源文献检索能力用 Python 重写进 Paperead，替换侧栏「参考文献引用助手」，并打通「检索 → 导入文献库 → 重排阅读」。

**Architecture:** `paperead/scholar/` 承载纯逻辑（源解析、缓存限流、推荐、引用），`server/scholar_api.py` 用 `build_router(store, add_pdf)` 注入文献库能力，`main.py` 挂载；前端新增 `ScholarPage` 与检索相关组件，沿用 `?v=search` 查询参数路由。

**Tech Stack:** Python 3.11 / FastAPI / httpx / stdlib xml.etree / pytest；React 19 / Vite / TypeScript / KaTeX。

---

### Task 1: 文本清洗与 Paper 模型

**Files:** Create `paperead/scholar/__init__.py`、`models.py`；Test `tests/test_scholar_sources.py`

- [ ] 写测试：`strip_latex_pointers(r"见 \cite{KL} 与 \ref{eq:1}")` → `"见 与"`；`decode_xml` 处理 `&amp;`/CDATA/标签；`clean_html_text` 把 `<h4>` 边界换成换行；`reconstruct_abstract({"a":[0],"b":[1]})` → `"a b"`；`epmc_key/split_epmc_key` 往返。
- [ ] 跑 `pytest tests/test_scholar_sources.py -v` 确认失败。
- [ ] 实现 `models.py`：`Paper` dataclass（含 `to_dict`）、`Source` 字面量、`SOURCE_LABELS`、`ARXIV_FIELDS`、上述清洗函数。
- [ ] 测试通过。

### Task 2: HTTP 层（缓存 / 合并 / 重试 / arXiv 节流）

**Files:** Create `paperead/scholar/http.py`；Test `tests/test_scholar_http.py`

- [ ] 写测试（asyncio）：同一 URL 并发 5 次只调用底层 1 次；TTL 过期后再调；抛错不写缓存；5xx 重试 2 次后抛 `FriendlyError`；arXiv 闸门保证两次调用间隔 ≥ 3s（用可替换时钟，测试里调小间隔）。
- [ ] 实现：`FriendlyError`、`RequestCache`（LRU 400 + inflight 合并 + TTL）、`fetch_text(url, ...)`、`arxiv_fetch(url, ...)`、`fetch_once`（超时）、`sleep` 退避。
- [ ] 测试通过。

### Task 3: 四个数据源的查询构造与解析

**Files:** Create `paperead/scholar/sources.py`；Test `tests/test_scholar_sources.py`（续）

- [ ] 写测试：arXiv 查询串 `ti:"a+b"+AND+cat:cs.CL+AND+submittedDate:[200001010000+TO+202612312359]`；Atom 片段解析出标题/作者/分类/doi/日期；OpenAlex results 解析（含 `primary_location`、`best_oa_location.pdf_url`、`cited_by_count`）；Europe PMC 解析出 `europepmc:PMC/PMC123` 与 OA PDF；PubMed esummary + efetch 摘要分节。
- [ ] 实现 `build_arxiv_query`、`parse_arxiv_entries`、`parse_openalex_works`、`parse_europe_pmc`、`parse_pubmed`。
- [ ] 测试通过。

### Task 4: 检索 / 详情 / 全文 / 中译英

**Files:** Create `paperead/scholar/search.py`；Test `tests/test_scholar_search.py`

- [ ] 写测试：`search()` 在 monkeypatch 掉 `fetch_text` 后按 source 分派且回填 `total/pageLimit`；Europe PMC `pageLimit = floor(200/per_page)`；`get_paper` 四种 id 前缀；`find_free_full_text` 404 返回空且不报错；`to_english_query` 纯英文不翻译、CJK 端点失败退回原文。
- [ ] 实现 `SearchInput`、`search()`、`get_paper()`、`find_free_full_text()`、`to_english_query()`。
- [ ] 测试通过。

### Task 5: 每日推荐

**Files:** Create `paperead/scholar/daily.py`；Test `tests/test_scholar_daily.py`

- [ ] 写测试：`heat_score` 对新论文更高、被引量为 None 时按纯时效；同 `date_key` 两次 `rank_daily` 结果一致；`daily_batch` 相邻 round 不重叠、绕回第一批；分类轮转不出现同一分类连续 3 条。
- [ ] 实现 `DAILY_BUCKETS`、`to_date_key`、`hash_seed`、`mulberry32`、`heat_score`、`rank_daily`、`daily_batch`、`build_category_or_query`、`build_openalex_subfield_filter`、`fetch_daily_pool`（含日缓存）。
- [ ] 测试通过。

### Task 6: 引用与导出

**Files:** Create `paperead/scholar/cite.py`；Test `tests/test_scholar_cite.py`

- [ ] 写测试：`split_name` 三种形态；GB/T 7714 / APA / MLA / IEEE 关键片段（作者截断、`等`、`et al.`、DOI 链接）；`build_bibtex` 的 `@misc`+`eprint`；`build_ris` 的 `TY  - GEN`。
- [ ] 实现 `CITATION_STYLES`、`split_name`、`format_citation`、`build_bibtex`、`build_ris`。
- [ ] 测试通过。

### Task 7: 路由与导入文献库

**Files:** Create `server/scholar_api.py`；Modify `server/store.py`（`add(..., meta)`、`find_by_source`）、`server/main.py`（include_router）；Test `tests/test_scholar_routes.py`

- [ ] 写测试：`build_router` 用假 store/add_pdf 构造 app，`/api/scholar/cite`、`/export` 正常；`/import` 假 PDF 走通、第二次同 source_id 返回 `already=true` 且不重复 add；无 `pdfUrl` 时返回 400 中文提示。
- [ ] 实现 `build_router(store, add_pdf, public)`：search / daily / paper / fulltext / import / cite / export；导入前 URL 校验（scheme、内网、大小、`%PDF`）。
- [ ] `main.py` 构造并挂载；`store.add` 支持 `meta`；`find_by_source` 去重。
- [ ] 测试通过，`pytest` 全绿。

### Task 8: 前端数学文本与 API 客户端

**Files:** Create `web/src/lib/math.ts`、`web/src/components/MathText.tsx`、`web/src/lib/scholar.ts`

- [ ] 移植 `splitMath`（`$$`/`\[`/`$`/`\(` + 裸 LaTeX 启发式）。
- [ ] `MathText` 用 KaTeX 渲染公式段，文本段支持 `renderText` 叠加高亮。
- [ ] `scholar.ts`：`ScholarPaper` 类型 + `searchPapers / getDaily / getPaper / findFreeFullText / importPaper / citePaper / exportPapers`，错误统一抛中文消息。

### Task 9: 检索页与详情页

**Files:** Create `ScholarPage.tsx`、`ScholarSearchBar.tsx`、`ScholarFilters.tsx`、`ScholarResult.tsx`、`ScholarDaily.tsx`、`ScholarDetail.tsx`

- [ ] `ScholarPage`：URL ↔ 状态同步（`v=search&q&src&sort&page&field&phrase&cat&from&to&per&pid`）、450ms 防抖检索、竞态保护、结果/推荐切换。
- [ ] `ScholarSearchBar`：来源标签、输入框、高级检索折叠、翻译回显。
- [ ] `ScholarResult`：来源·分类·被引、标题与摘要高亮、悬停展开摘要。
- [ ] `ScholarDaily`：日期、分类说明、换一批。
- [ ] `ScholarDetail`：元信息、摘要、导入并阅读（成功后 `onOpenPaper`）、下载 PDF、免费全文、复制引用下拉、原文页。
- [ ] `tsc` 通过。

### Task 10: 接线与样式

**Files:** Modify `web/src/App.tsx`、`web/src/components/Sidebar.tsx`、`web/src/styles.css`、`README.md`

- [ ] `App.tsx`：`v=search` → ScholarPage；`?p=` 仍是阅读页；记录「从检索页进入」以便返回。
- [ ] `Sidebar.tsx`：导航改为 文献库 / 文献检索 / 研究（禁用）/ 写综述报告（禁用），`onNav` 回调。
- [ ] `styles.css`：`.scholar-*` 样式（搜索框、来源标签、筛选网格、结果行、摘要 mark、详情卡、推荐区），沿用 `--blue/--line/--muted` 等变量。
- [ ] `npm run build` 通过。

### Task 11: 验证与文档

- [ ] `pytest -q` 全绿。
- [ ] `cd web && npm run build` 通过。
- [ ] 启动 `python -m server.main`，`curl /api/scholar/search?q=graph+neural+network&source=arxiv` 与 `/api/scholar/daily` 有结果（无网时用 mock 测试兜底并在 README 注明）。
- [ ] README 增加「文献检索」一节与新接口表；侧栏截图说明更新。
