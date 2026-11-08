# Paperead · 从「读完」到「搞懂」

> 定位：不是论文阅读器，是**论文理解助手**。
> 阅读是手段，搞懂才是目的。

- 版本：v0.2（设计稿）
- 日期：2026-11-08
- 关联：`README.md`（阅读器能力）、`docs/preview-*.png`（现有界面）

> **实现状态**
>
> | 阶段 | 状态 | 说明 |
> |---|---|---|
> | P0 导读卡 | ✅ 已实现 | `paperead/tutor/{concepts,references,prereq,guide}.py` + `GET/POST /guide` + 前端「导读」tab |
> | P1 追本溯源 | ✅ 已实现 | `citations.py` + `provenance.py` + `GET /block/{id}/provenance` + 段落「引」按钮 |
> | P2 脉络图 | ✅ 已实现 | `lineage.py` + `GET/POST /lineage` + 前端「脉络」tab（竖向时间轴） |
> | P3 三路问答 / 自测 | 🟡 问答已完成，自测未做 | `qa.py` + `web.py` + `POST /ask` + 前端问答 tab |
>
> **实现时的偏差**：§7.2 原设计是"全局检索 + 引用网络"，实测用概念去 OpenAlex 检索会返回
> 无关的高被引论文（`regulation type` → 《Hallmarks of Cancer》）。改为**只在本文参考文献里做
> 文本匹配 + 年份过滤**，质量与成本都更好。OpenAlex 检索挪到 P2（脉络图需要引用关系时再用）。

---

## 1. 背景

### 1.1 现状

现在的 Paperead 解决的是「**把论文排好、翻成中文、能一段段读**」：

- 双栏重排、图表公式裁切、按页流式翻译
- 左侧阅读助手（问答 / 目录 / 重点 / 图表）

这是**阅读器**的能力边界：让用户把一篇论文读完。

### 1.2 真实痛点

用户的原话值得记录：

> 市面上这些软件都有个缺陷——没有跳出阅读器这个框架。你只能用它来阅读，但它没法成为你的老师。
> 遇到一篇怎么读都读不懂的文章，还得切出去找别的 AI 问。
> 如果它能给我可视化的梳理、追本溯源，甚至**在读之前就建议我先读这个领域的哪几篇**，那就不一样了。

拆解成三个具体场景：

| 场景 | 用户行为 | 现在的产品 |
|---|---|---|
| **读前**：这篇我该不该读、读得懂吗 | 打开摘要扫两眼，发现看不懂就关了 | 只给摘要 |
| **读中**：这句话说的 `[31]` 到底是什么 | 切浏览器搜作者年份，来回折腾 | 点开是纯文本，没有出处 |
| **读后**：我算读懂了吗 | 说不清，只能再问一遍 AI | 没有任何检验 |

### 1.3 我们要回答的问题

> 用户不是要「读完一篇论文」，是要「**能就这篇论文和人讨论**」。

---

## 2. 「懂」的可操作定义

把"懂"拆成四个可验收的问题，每个问题对应一个产品能力：

| # | 问题 | 用户内心 | 对应能力 |
|---|---|---|---|
| Q1 | 它解决什么？以前的做法为什么不行？ | "这篇在跟谁较劲" | **导读卡** |
| Q2 | 每个设计为什么这么做？能不能删掉？ | "这个模块是干嘛的" | **脉络 + 追本溯源** |
| Q3 | 证据够不够？这个 SOTA 是不是刷的？ | "我该信几分" | **三路问答** |
| Q4 | 它从哪来？我该先读什么？ | "我基础不够吧" | **前置知识 + 阅读路径** |

**验收标准**：读完导读 + 原文后，用户能不看原文回答 Q1 和 Q4；能指着任意一段说出它的出处（Q2）；能对结论给出保留意见（Q3）。

---

## 3. 用户与场景

### 3.1 三类用户

| 类型 | 特征 | 最需要 |
|---|---|---|
| **跨领域研究者** | 生物/材料背景，第一次读图学习论文 | 前置知识、术语解释、脉络 |
| **低年级研究生** | 知道领域，但读不懂顶会细节 | 追本溯源、方法取舍 |
| **工程实践者** | 想知道"能不能用、怎么用" | 摘要 + 结论 + 复现难度 |

### 3.2 关键用户旅程

```
上传 PDF
  ↓ 30s
导读卡：这是什么、需要什么前置、建议先读哪几篇
  ↓ 用户选择「直接读」或「先补前置」
阅读（现有的双栏 + 翻译 + 按页流式）
  ↓ 遇到看不懂的段落
点 ⓘ 溯源：这句话来自哪篇、原文怎么说
  ↓ 还是不懂
左栏问答：三路检索（本文 / 引用文献 / 外部）
  ↓ 读完
5 题自测：答错跳回对应段落
```

---

## 4. 功能设计

### 4.1 导读卡（P0）

**触发**：解析完成即生成（后台），阅读页「导读」tab 展示；首次进入默认停留在此 tab。

**内容**：

```
1. 一句话总结      ≤ 60 字，说清 who/what/why
2. 领域标签        2–4 个
3. 难度评估        数学密度 / 术语密度 / 篇幅 → 1–5 星 + 预计阅读分钟
4. 前置知识清单    3–6 条，每条：概念名 + 为什么需要 + 可选「已掌握」
5. 推荐阅读顺序    0–3 篇外部文献 + 本文，每篇给"读完你能获得什么"
6. 三个动作        直接读 / 先补前置 / 只读关键章节
```

**交互细节**：

- 勾选「已掌握」→ 该条从清单移除，且不再出现在路径里
- 「先补前置」→ 展示推荐文献卡片（标题/年份/被引/开放获取链接）
- 导读卡可折叠，折叠后顶栏留一个「导读」入口

**质量要求**：每条前置推荐必须写清"为什么"，且必须是**本文实际引用过或同领域高被引**的文献，不允许凭空推荐。

### 4.2 前置知识与阅读路径

见 §7.2 / §7.3 的算法。

**阅读路径的三种形态**：

| 用户状态 | 路径 |
|---|---|
| 全部前置已掌握 | 直接本文 |
| 缺 1–2 个 | 缺哪个补哪个（单篇） |
| 完全跨领域 | 综述 → 关键方法 → 本文（3 步） |

### 4.3 思路来源图（P2）

> 不做"引文网络"。引文网络只说明"谁引用谁"，说明不了**"作者是怎么想到这个主意的"**。
> 这张图要回答三个问题：
>
> 1. 这篇论文的每个关键部件，是**从哪篇**来的？
> 2. 作者对它们**改了什么**？（这是本文的贡献本身）
> 3. 如果我要读懂它的动机，**该先看哪几篇**？

#### 4.3.1 信息模型

**节点角色**（决定纵向分栏）：

| 角色 | 含义 | 判定信号 |
|---|---|---|
| `foundation` 基础 | 领域定义、综述、公认框架 | 综述/定义类引用；出现在 Introduction 开头 |
| `inspiration` 直接启发 | 本文直接借用了它的思路/模块 | 引用句含 "based on / follow / inspired / extend" |
| `contrast` 对比/不足 | 本文指出它的问题，从而引出自己的做法 | 引用句含 "however / unlike / limited / fail" |
| `tool` 沿用 | 数据集、评测协议、工具 | 出现在 Experiments；数据集名 |

**边**：`ref → 本文`，标签是**作者自己说过的关系**，例如"沿用其消息传递框架""指出其感受野受限"。

**关键约束**：每条边必须绑定**本文中引用该文献的那句话**（evidence）。没有 evidence 的文献不进图——
这样图上的每一根线都能点开看到原文依据，不会变成模型编的因果。

#### 4.3.2 生成流程

```
① 选点
   统计每个参考文献在正文被引用的次数
   score = 次数 × 章节权重（Intro/Method 1.0，Experiment 0.6）
   取 top 10–12，且至少覆盖 3 个角色类别

② 取证据
   对每篇文献，取所有引用它的段落，抽出标记所在句子 ± 上下文 1 句

③ 定角色与"借了什么/改了什么"（1 次 LLM，批量）
   输入：文献标题 + 年份 + evidence 句子
   输出：{role, borrow, improve}
   校验：role 必须在枚举内；borrow 为空则丢弃该节点

④ 布局
   x = 年份（左旧右新），y = 角色泳道
   本文固定在最右侧，边从左指向它
```

LLM 失败或全部校验不过时，按关键词规则降级（`however`→contrast，`based on`→inspiration，`survey`→foundation），
`borrow` 用 evidence 原文截断。

#### 4.3.3 视觉

```
 基础     ○ GNN综述 2020 ─────────────┐
                                      │ 沿用消息传递框架
 启发     ○ SEAL 2018 ────────────────┼──▶  ◆ 本文 2024
                              ┌───────┘ 指出其感受野受限
 对比     ○ ID-GNN 2021 ─────┘
 沿用     ○ DREAM5 2012 ──────────────┘ 沿用评测数据
        ────┬────────┬────────┬──────────────▶ 年份
          2012     2018     2021      2024
```

- 节点形状区分角色：圆=基础、方=启发、菱形=对比、六边形=沿用
- 连线样式：实线=借用/沿用、虚线=对比、点线=弱相关
- hover 高亮该节点及其边；click 打开详情
- 默认只显示前 8 个节点，其余收进"+3 篇"

#### 4.3.4 详情面板

点击节点后展示：

```
SEAL (Zhang 2018)                       [直接启发]
它给了本文什么
  子图采样 + DRNL 节点标记，用于让 GNN 感知节点对的结构位置
本文改了什么
  SEAL 只编码路径深度，不编码"宽度"（节点度、路径数）；
  本文用二元结构 Transformer 显式建模成对结构
原文依据
  "…SEAL calculates DRNL of all nodes in an enclosing subgraph."
  [查看该段]  [打开原文]
```

#### 4.3.5 数据与接口

```sql
lineage_nodes(id, paper_id, ref_index, title, year, role, borrow, improve, evidence, cites)
lineage_edges(id, paper_id, ref_index, relation, weight)
```

```
GET /api/papers/{id}/lineage → { self, nodes: [], edges: [], yearRange: [min, max] }
```

结果缓存到 `{paper_id}.lineage.json`。

#### 4.3.6 成本与验收

| 项 | 目标 |
|---|---|
| 生成耗时 | < 8s（1 次 LLM） |
| 成本 | ≈ ¥0.02/篇 |
| 边有 evidence 的比例 | 100%（硬约束） |
| 角色判定人工一致率 | ≥ 80%（抽 20 篇） |
| "能说清本文从谁受启发" | 用户测试：看完图能复述 ≥ 2 条启发 |

### 4.4 追本溯源（P1）

**入口**：段落/公式/图注 hover 出现 `ⓘ`；有引用的段落常驻一个小圆点。

**浮层内容**：

```
[31] Gilmer et al. 2017 · Neural Message Passing for Quantum Chemistry
"…we define message passing as…"     ← 原文片段（可展开）
再往上：这一思想最早可追溯到 [Spektral 2015]
[在 PMC 阅读原文]  [加入待读]
```

**两跳策略**：第一跳 = 本文直接引用；第二跳 = 该文献的关键引用（回答"最早是谁提出"）。

### 4.5 三路问答（P3）

```
用户提问
 ├─ 本文 RAG       blocks（含译文）+ 页码定位
 ├─ 引用文献检索   本文 references 范围内检索
 └─ 外部检索      PubMed / arXiv / OpenAlex / 网页
        ↓
   统一重排（相关性 + 权威性 + 时效）
        ↓
   生成：每个论断带出处徽标 [本文 P3] [Zhang 2018] [PubMed]
```

**必须做**：回答里出现的数字/结论，必须能点到出处；找不到出处就明说"论文里没提"。

### 4.6 读后自测（P3）

- 5 题：概念辨析 ×2、方法取舍 ×1、实验解读 ×2
- 答错 → 显示"回看第 X 页第 Y 段"，并高亮原文
- 记录得分，作为"搞懂"的量化指标

---

## 5. 信息架构

### 5.1 页面结构

```
文献库（首页）
 └─ 阅读页
     ├─ 顶栏：返回 / 标题 / 模式 / 字号 / 翻译进度 / 未配置提示 / 设置
     ├─ 左栏（6 tab）
     │   ├─ 导读     ← 新增，默认
     │   ├─ 问答
     │   ├─ 目录
     │   ├─ 脉络     ← 新增
     │   ├─ 重点
     │   └─ 图表
     └─ 正文（现有）
```

### 5.2 状态

`paper.guide_status`: `none → generating → ready | failed`

导读生成失败不影响阅读；tab 内显示重试按钮。

---

## 6. 技术架构

### 6.1 模块划分

```
paperead/
  paperead/
    layout.py  translate.py  formula.py  llm.py   （已有）
    tutor/                                        （新增）
      concepts.py    概念抽取（术语/方法/数据集/符号）
      prereq.py      前置文献匹配（引用网络 + 检索）
      guide.py       导读卡生成（LLM + 结构化输出）
      graph.py       脉络图构建（references/citations）
      provenance.py  引用解析 → 原文片段
      qa.py          三路 RAG
      search.py      检索适配层（OpenAlex/S2/主项目）
  server/
    main.py         新增 /guide /graph /provenance /ask 端点
  web/
    components/     GuidePanel / GraphPanel / ProvenancePopover
```

### 6.2 复用现有能力

| 能力 | 来源 | 用法 |
|---|---|---|
| 多源检索 | `backend/search/engines/*` | PubMed / arXiv / OpenAlex / S2 / PMC |
| 全文抓取 | `backend/search/fulltext.py` | PMC / arXiv 原文段落 |
| 统一外呼 | `backend/search/http.py` | 限流、重试、代理 |
| 术语表 | `paperead.translate.build_glossary` | 概念抽取的种子 |
| LLM | `paperead.llm` | OpenAI 兼容，可注入 |

**独立性**：新项目仍需能独立运行。`search.py` 做成适配器：

```python
class SearchBackend(Protocol):
    async def references(self, doi: str) -> list[Ref]: ...
    async def by_title(self, title: str) -> list[Ref]: ...
    async def abstract(self, doi_or_id: str) -> str | None: ...
```

默认实现直连 OpenAlex（免费、限流宽松）；配置 `PAPEREAD_SEARCH_BASE` 时走主项目。

### 6.3 数据流

```
上传 → 解析（blocks）
         ↓
     概念抽取 ──▶ paper_concepts
         ↓
   前置匹配（并发 6 路检索）──▶ prereq_edges
         ↓
     导读生成（1 次 LLM）──▶ reading_plans
         ↓
      阅读页「导读」tab
```

导读生成与解析解耦：上传即可看正文，导读 10s 内补齐。

---

## 7. 关键算法

### 7.1 概念抽取（concepts.py）

输入：`blocks`（含术语表）。
输出：`[{name, kind, section, importance, evidence}]`

步骤：

1. **种子**：术语表（已由翻译流程产出）+ 各级标题里的名词短语
2. **方法/数据集**：正则 + 词表（`Dataset`、`-Net`、`-Former`、全大写缩写）
3. **重要性**：该概念在全文出现次数 × 是否出现在标题/摘要 × 是否出现在图注
4. **过滤**：保留 importance ≥ 阈值的 8–12 个

> 不追求完美，导读只需要"用户最可能卡住的那几个"。

### 7.2 前置文献匹配（prereq.py）

对每个概念：

```python
candidates = [ref for ref in paper.references
              if concept in ref.title+abstract or ref.is_survey]
candidates += search.by_title(f"{concept} survey")[:5]

score = 0.4 * 引用次数(归一)
      + 0.3 * 时间更早
      + 0.2 * 是否本文引用
      + 0.1 * 是否综述

pick top1 → LLM 写一句 "为什么读它"
```

**硬约束**：不能推荐本文没有引用、且不在同领域高被引前 1% 的文献（防幻觉）。

### 7.3 阅读路径（path.py）

```
若 前置为空            → [本文]
若 缺 1–2 个           → [对应文献…, 本文]
若 缺 ≥3 个            → [综述, 关键方法 ×1–2, 本文]
```

排序约束：综述在前、奠基方法在中、本文最后；总时长控制在 90 分钟以内。

### 7.4 脉络选点（graph.py）

```python
refs = openalex.references(doi)          # 40–80 篇
freq = count_in_text(refs, blocks)       # 正文引用次数
score = 0.5*norm(freq) + 0.3*norm(cited_by) + 0.2*is_survey
keep = top(score, 12)
```

连线：`A cites B` 实线；embedding 相似度 > 0.8 且无引用关系 → 虚线。

### 7.5 引用解析（provenance.py）

难点：`[31]` 只是正文编号，要落到真实文献。

```
1. 解析本文参考文献块（blocks 里 kind=reference）
2. 用正则切出条目：序号 + 作者 + 年份 + 标题 + DOI/arXiv
3. 校验：条目数 vs 正文最大编号，不匹配则降低置信度
4. 置信度低 → 不展示溯源入口（宁缺毋滥）
```

### 7.6 三路问答（qa.py）

```python
docs = []
docs += paper_blocks_retrieval(query, top_k=6)      # 本文
docs += cited_papers_retrieval(query, top_k=4)      # 引用文献摘要
docs += external_search(query, top_k=4)             # PubMed/arXiv/OpenAlex

rerank(docs)  # 相关性 0.6 + 权威性 0.25 + 时效 0.15
answer = llm(system=引用约束, context=docs)
```

---

## 8. 数据模型

```sql
paper_concepts(
  id, paper_id, name, kind, section, importance, evidence_json, created_at
)

prereq_edges(
  id, paper_id, concept_id, source_paper_id, title, doi, year,
  reason, "order", score, created_at
)

reading_plans(
  id, paper_id, summary, domain_json, difficulty_json,
  steps_json, generated_at, status, error
)

claim_citations(
  id, paper_id, block_id, cite_index, doi, title, snippet, hop, created_at
)
```

存储沿用 `data/papers/{id}.*` 的思路：导读与概念写成 `{id}.guide.json`，方便整体迁移。

---

## 9. API 契约

```
POST /api/papers/{id}/guide          生成（幂等，已生成直接返回）
GET  /api/papers/{id}/guide          读取

GET  /api/papers/{id}/concepts       概念列表
POST /api/papers/{id}/concepts/{cid}/mastered   标记已掌握

GET  /api/papers/{id}/graph          脉络图（nodes + edges）
GET  /api/papers/{id}/block/{bid}/provenance    段落溯源

POST /api/papers/{id}/ask            三路问答（SSE，带出处）
```

`/guide` 响应：

```json
{
  "status": "ready",
  "summary": "用二元结构 Transformer 补 GNN 在链接预测上看不见节点对结构关系的短板。",
  "domains": ["图表示学习", "链接预测"],
  "difficulty": {"stars": 3, "minutes": 35, "math": "high", "terms": "medium"},
  "prereqs": [
    {"concept": "注意力机制", "why": "本文的 BST 完全基于自注意力",
     "paper": {"title": "Attention Is All You Need", "year": 2017, "cites": 120000},
     "mastered": false}
  ],
  "path": [
    {"kind": "survey", "title": "GNN 综述 (Zhou 2020)", "minutes": 15, "gain": "建立整体框架"},
    {"kind": "method", "title": "SEAL (Zhang 2018)", "minutes": 30, "gain": "理解子图 + DRNL"},
    {"kind": "paper", "title": "本文", "minutes": 35, "gain": ""}
  ]
}
```

---

## 10. Prompt 设计（关键约束）

### 10.1 导读生成

```
你是论文导读助手。基于给定的论文标题、摘要、章节标题、术语表，
生成一份 3 分钟导读。

硬约束：
1. summary ≤ 60 字，必须包含"解决什么问题 + 用了什么方法"
2. prereqs 只从给定的候选概念里选，最多 6 个
3. 每条 prereq 的 why 必须指向本文的具体章节或模块
4. 不要评价论文好坏，不要编造数字
5. 输出 JSON，不要解释
```

### 10.2 前置推荐理由

```
给定概念「子图采样」和候选论文，用一句话说明：
读完这篇能让读者获得什么，才能读懂本文的哪一部分。
≤ 40 字，不要复述标题。
```

### 10.3 溯源片段定位

```
给定句子 S 和候选文献摘要/全文片段，找出与 S 最相关的一段原文。
如果都不相关，输出 NONE。
不要改写原文，原样摘录。
```

---

## 11. 成本与延迟

| 步骤 | 耗时 | 成本 | 触发 |
|---|---|---|---|
| 概念抽取 | < 1s | 0（本地） | 解析后 |
| 前置匹配（6 路并发检索） | 2–4s | 0（OpenAlex 免费） | 解析后 |
| 导读生成（1 次 LLM） | 3–5s | ≈ ¥0.02 | 解析后 |
| 脉络（2 次 API + 1 次 LLM） | 3–5s | ≈ ¥0.01 | 打开脉络 tab |
| 溯源 | 2–3s | ≈ ¥0.005/次 | 点击时 |
| 自测 5 题 | 8s | ≈ ¥0.02 | 读完后 |

一篇论文的完整"搞懂"成本 < ¥0.1。

---

## 12. 分期计划

| 阶段 | 内容 | 工作量 | 验收 |
|---|---|---|---|
| **P0** | 概念抽取 + 前置匹配 + 导读卡（后端 + 前端 tab） | 3–4 天 | 10 篇论文：前置推荐人工判断必要率 ≥ 80% |
| **P1** | 追本溯源（引用解析 → 原文片段） | 3 天 | 30 个引用句，出处正确率 ≥ 85% |
| **P2** | 脉络图 | 4 天 | 选点合理，点击可达 |
| **P3** | 三路问答 + 自测 | 3 天 | 答错能定位回原文 |

**先做 P0 的理由**：它单独就能回答"我该先读什么"，不依赖新基础设施，且是所有后续能力的数据底座（概念表）。

---

## 13. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 前置推荐不准 | 用户不信任 | 只推本文引用过的 + 高被引 + 更早的；必须给理由；可反馈"不需要" |
| 引用编号错位 | 溯源指错文献 | 双向校验，置信度低就不显示 |
| 外部 API 限流 | 导读生成失败 | OpenAlex 优先 + 本地缓存 24h + 失败降级为"仅本文信息" |
| LLM 幻觉 | 编造前置文献 | 候选集由检索给定，模型只能选不能造 |
| 导读太长没人看 | 功能白做 | 首屏 ≤ 3 屏；详细内容折叠 |

---

## 14. 不做的事

- **不做全自动文献综述**（那是 Elicit 的地盘，且质量不可控）
- **不做文献管理**（Zotero 已经做得很好，我们只做导入）
- **不做"论文评分"**（容易误导，也不解决"懂"的问题）
- **不在阅读时实时调用外部检索**（除了用户主动问）——成本与延迟不可控

---

## 15. 度量

**北极星指标**：`读完一篇论文的用户，自测正确率 ≥ 70% 的比例`

辅助指标：

| 指标 | 目标 |
|---|---|
| 导读卡打开率 | > 80% |
| 前置推荐采纳率（点"先读"） | > 25% |
| 段落溯源点击率 | > 10% |
| 读后提问数 | ≥ 2 次/篇 |
| 二次阅读率 | > 40% |

---

## 16. 下一步

1. 评审本设计
2. 按 P0 开工：`tutor/concepts.py` → `tutor/prereq.py` → `tutor/guide.py` → `server/main.py` → 前端「导读」tab
3. 用 5 篇真实论文跑通，人工评估前置推荐质量后再进入 P1
