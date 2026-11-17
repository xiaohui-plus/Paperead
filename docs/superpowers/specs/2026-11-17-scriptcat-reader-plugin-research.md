# 脚本猫阅读插件支线研究

- 日期：2026-11-17
- 状态：研究结论，待决策
- 范围：在脚本猫（ScriptCat）用户脚本形态下，把 Paperead 的阅读能力（划词翻译、正文问答、批注、高亮、引用）带入 arXiv / PubMed / PMC / 出版商页面
- 不在范围：MV3 扩展的重构、移动端、云端同步

## 0. 结论摘要

1. **脚本猫适合做"页面内快操作"，不适合承载完整阅读器。** 它没有侧边栏 API，不能运行在 Chrome 内置 PDF 查看器里，SSE 流式能力受限，严格 CSP 站点注入有风险。
2. **最省力且体验最好的组合是"内联工具条 + 新标签完整阅读"**：
   - 页面内：划词翻译、选中问答、复制引用、保存入库，用轻量浮动面板完成，不复制阅读器 UI。
   - 需要完整阅读（PDF 渲染、目录、整篇译文、批注管理）时，用 `GM_openInTab` 打开本地阅读页，深链接定位到对应论文与段落。
3. **不建议第一版做 iframe 嵌入式阅读器。** 技术上可行（服务端没有 `X-Frame-Options` / `frame-ancestors`），但要额外解决 Chrome Local Network Access 授权、第三方存储分区导致的登录态丢失、以及主 token 不能暴露给宿主页面这三个问题，收益不如新标签页直接。
4. **接口层需要新增"阅读作用域"**：现有插件 token 只有 `library:read/write`，且只能访问 `/api/ext/v1`。要复用 `/api/translate/text`、`/api/papers/{id}/ask`、`/api/papers/{id}/annotations`，必须在 `/api/ext/v1` 下提供受控代理，或给插件 token 增加 `reading:*` 作用域并放行白名单端点。
5. **批注双向同步是最高价值、也是最高风险的部分。** 需要把外部页面的选区锚定到本地论文的 block 上，第一版建议只做"外部选区 → 本地批注"单向写入，不做冲突合并。
6. **竞品调研后，P0 增加三项基础能力**：HTML 优先注入、DOI 开放全文解析、translator 式站点适配器注册表。缺少这三项，脚本只能在 arXiv 摘要页工作，覆盖面不足以支撑一次真实试用。
7. **模型配置直接复用桌面端「设置 → 模型服务」**：脚本不持有 API key，也不提供第二套模型设置界面；`reading/status` 只回报是否就绪与模型名称。

## 1. 脚本猫能力与边界

### 1.1 运行模型

- ScriptCat 是开源 MV3 用户脚本管理器，兼容油猴脚本头与 GM API，额外支持 `@background` 后台脚本、`@crontab` 定时脚本。
- 脚本运行在页面上下文（`@inject-into content` 时为隔离环境），可读写 DOM、监听选区与键盘事件。
- Chrome 138+ 对用户脚本有额外开关，Chrome 端还需开发者模式或允许用户脚本；这会影响普通用户的安装成功率。

### 1.2 GM API 可用性

| API | 可用 | 说明 |
|---|---|---|
| `GM_xmlhttpRequest` | 是 | 扩展后台代发，需 `@connect`；`data` 支持 FormData / Blob / File / ArrayBuffer；`responseType` 支持 arraybuffer / json / stream |
| `GM_setValue` / `GM_getValue` | 是 | v0.17 起存 `chrome.storage.local`，异步写入；适合存 token、端口、缓存 |
| `GM_notification` | 是 | 支持进度与按钮，用于解析完成提示 |
| `GM_registerMenuCommand` | 是 | 可做右键菜单式的脚本命令 |
| `GM_addElement` / `GM_addStyle` | 是 | 可绕页面 CSP 注入 UI |
| `GM_openInTab` | 是 | 打开本地阅读页的关键能力 |
| `GM_setClipboard` | 是 | 复制引用、复制译文 |
| `GM_download` | 是 | 下载 PDF，但受站点权限与登录态限制 |

限制：

- 大文件不流式：内部会先转 Blob，跨 content ↔ background 走 base64 或 blob URL，内存开销大。100MB 级 PDF 应交给本地服务下载，而不是脚本中转。
- 无全局快捷键，只能绑定页面内 `keydown`。
- 无侧边栏 API，脚本只能自绘悬浮面板。
- SSE 不能直接消费。`GM_xmlhttpRequest` 的 `stream` 响应在脚本侧处理复杂，第一版问答走非流式接口。

### 1.3 网络、Origin 与本地服务

- `GM_xmlhttpRequest` 由扩展后台发出，Origin 通常是 `chrome-extension://<id>` 或缺失，不受页面 CORS 约束。
- 因此本地服务不需要为脚本配置 ACAO；现有的「无 Origin 也允许配对」改造已经覆盖这一点。
- **Chrome Local Network Access（LNA）是新增变量**：公共网页向 `127.0.0.1` 发请求或做子框架导航，可能需要用户授予 loopback 网络权限。扩展后台请求是否完全豁免尚未证实，需要在 Chrome 142+ 实测。
- 若 LNA 拦截，表现为请求直接失败而不是 403，脚本必须给出可读提示，不能只说「网络错误」。

### 1.4 注入限制

- **Chrome 内置 PDF 查看器（`chrome-extension://mhjf...`）无法注入**，读不到选区、页数，也不能加悬浮按钮。PDF 场景只能拦截链接跳转，或改用本地阅读页的 pdf.js。
- 严格 CSP / Trusted Types 站点（部分出版商）：`GM_addElement`、`GM_addStyle` 可绕过 CSP，但 Trusted Types 仍可能报错；脚本需要降级为纯文本面板。
- 出版商常把正文放在 iframe 或 Shadow DOM 中，`@match` 要覆盖子框架（脚本当前是 `@noframes`，需要调整）。

### 1.5 市场与更新

- scriptcat.org 发布与更新走人工审核，社区反馈排队数日至十余天，存在「审核中 / 未通过」状态。
- 客户端按 `@version` 轮询更新，不是实时推送。
- 市场禁止混淆与远程私有代码；广告、付费需标注 `@antifeature`。
- 结论：脚本适合内测快速迭代，但正式发布仍需预留审核周期。

### 1.6 能力矩阵

| 能力 | 脚本猫 | 说明 |
|---|---|---|
| 页面元数据提取 | 强 | 直接读 DOM，比扩展更简单 |
| 同源 PDF 获取 | 强 | 页面内 `fetch` 天然带登录态 |
| 调本地 API | 强 | `GM_xmlhttpRequest` + `@connect` |
| 悬浮面板 / 工具条 | 中 | 自绘，Shadow DOM 隔离 |
| 侧边栏 | 无 | 只能自绘或新标签页 |
| 后台常驻 | 弱 | 页面关闭即停止；`@background` 可做但非标准 |
| 流式输出 | 弱 | 非流式接口兜底 |
| PDF 页内交互 | 无 | 查看器不可注入 |
| 批注锚定 | 中 | 依赖文本匹配，需处理站点差异 |
| 安装门槛 | 中 | 先装 ScriptCat，再装脚本 |

## 2. Paperead 可复用能力盘点

### 2.1 阅读页与深链接

- 阅读页地址：`http://127.0.0.1:<port>/?p=<paper_id>`，由 `server/ext_api.py` 的 `open_url` 返回。
- `web/src/components/ReaderView.tsx` 读取 URL 参数控制视图：`view=pdf`、`tab`、`full`，并支持按 block / page 定位与滚动。
- 结论：脚本可以通过 `?p=<id>&tab=chat` 之类参数，把用户直接送到问答或 PDF 视图，不必自己实现阅读器。

### 2.2 可复用的后端接口

| 能力 | 端点 | 关键输入 | 备注 |
|---|---|---|---|
| 论文元数据 / 状态 | `GET /api/papers` | — | 列表，含状态与标题 |
| PDF 原文流 | `GET /api/papers/{id}/file` | — | 浏览器可直接打开 |
| 结构化正文 | `GET /api/papers/{id}/blocks` | — | 段落 / 块，是批注锚定的基础 |
| 导读 | `GET/POST /api/papers/{id}/guide` | — | 摘要、贡献点 |
| 划词翻译 | `POST /api/translate/text` | `text`（≤6000 字）、`target_lang`、`config` | 非流式，适合选区翻译 |
| 整篇翻译 | `POST /api/papers/{id}/translate` | 目标语言等 | 有流式版本 `/translate/stream` |
| 问答（非流式） | `POST /api/papers/{id}/ask` | `question`、`web`、`quotes`、`block_ids`、`config` | 脚本第一版用它 |
| 问答（流式） | `POST /api/papers/{id}/ask/stream` | 同上 | SSE，脚本侧不优先 |
| 对话历史 | `GET /api/papers/{id}/chat` | — | 可回放 |
| 批注列表 / 新建 | `GET/POST /api/papers/{id}/annotations` | `block_id`、`quote`、`start/end`、`color`、`kind`、`note` | 支持段落锚点与 PDF 页锚点 |
| 批注修改 / 删除 | `PATCH/DELETE /api/papers/{id}/annotations/{aid}` | — | — |
| 高亮 | `GET/POST /api/papers/{id}/highlights` | 同上 | — |
| 阅读进度 | `PUT /api/papers/{id}/progress` | — | 可用于继续阅读 |
| 引用导出 | `POST /api/scholar/cite`（scholar_api） | — | 需确认字段 |
| 查重 / 入库 | `GET /api/ext/v1/papers/lookup`、`POST /api/ext/v1/papers/ingest` | — | 已实现 |

### 2.3 鉴权现状

- 主应用：`server/main.py` 的 `guard` 校验 `pr_token`（cookie / header / query 三种来源）。
- 插件：`/api/ext/v1` 业务接口校验 `Authorization: Bearer <plugin token>`，token 只存 SHA-256。
- 阅读类接口（`/api/translate/text`、`/ask`、`/annotations`）目前**只认主 token**，插件 token 无法调用。
- 这是脚本接入阅读功能必须先解决的第一道门。

### 2.4 前端耦合度

- `Reader.tsx` 把翻译、批注、高亮、问答上下文、划词提问作为 props 接收，说明这些能力本身是组件化的。
- 但 `ReaderView.tsx` 持有论文状态、翻译进度、PDF 页码、自动翻译设置等全局状态，与 App 的会话、设置、事件系统耦合。
- 结论：**不适合抽一个独立"阅读器 SDK"给脚本用**；复用方式应该是"打开本地阅读页"，而不是把 React 组件打包进用户脚本。

## 3. 四种结合形态对比

### A. 内联轻量面板（脚本自绘 UI + 受控 API）

页面右下角悬浮按钮，选中文字后出现工具条：翻译、提问、高亮、复制引用、保存。

- 优点：完全在论文页内，零跳转；实现量可控；不复制阅读器。
- 缺点：需要新增阅读作用域接口；问答非流式；批注需要文本锚定。
- 适合：第一版。

### B. iframe 嵌入完整阅读页

在页面右侧插入 iframe，指向 `http://127.0.0.1:<port>/?p=<id>`。

- 优点：功能最全，零 UI 重复开发。
- 缺点：
  - 主 token 若拼进 URL，会暴露给宿主页面的 DOM 与日志；
  - 第三方存储分区导致 cookie 不携带，需要一次性 ticket；
  - Chrome LNA 可能要求用户授权 loopback 子框架；
  - 页面宽度被挤压，阅读体验差。
- 适合：第二版，且只在"本地服务同机、用户已授权"时启用。

### C. 打开独立阅读标签页

`GM_openInTab('http://127.0.0.1:<port>/?p=<id>')`。

- 优点：一行代码，安全边界清晰，功能完整。
- 缺点：离开当前页面。
- 适合：作为 A 的兜底与"完整阅读"入口。

### D. 混合（推荐）

- 页面内做高频轻操作：划词翻译、选中提问、复制引用、保存。
- 需要深读时一键打开本地阅读页，并带上定位参数。
- 后续再做可选侧栏 iframe。

| 维度 | A 内联面板 | B iframe | C 新标签 | D 混合 |
|---|---|---|---|---|
| 开发量 | 中 | 高 | 低 | 中 |
| 安全风险 | 低 | 中高 | 低 | 低 |
| 功能完整度 | 中 | 高 | 高 | 高 |
| 页面内体验 | 好 | 中 | 差 | 好 |
| 受 LNA 影响 | 小 | 大 | 无 | 小 |

## 4. 推荐架构

```text
arXiv / PubMed / 出版商页面
        │
        │ 用户划词 / 点击悬浮按钮
        ▼
脚本猫用户脚本（Shadow DOM 工具条 + 面板）
   ├─ 页面识别：DOI / arXiv / PubMed / PMC → lookup 本地库
   ├─ 未入库：ingest（复用现有采集链路）
   ├─ 已入库：拿到 paper_id
   ├─ 划词翻译 → /api/ext/v1/reading/translate
   ├─ 选中提问 → /api/ext/v1/reading/ask
   ├─ 新建批注 → /api/ext/v1/reading/annotations
   └─ 完整阅读 → GM_openInTab(open_url + 定位参数)
        │
        ▼
本地 Paperead（127.0.0.1:17321）
   └─ reading 代理层：校验插件 token 与作用域，转发到现有业务函数
```

关键设计：**不要让插件 token 直接打主 `/api`**。在 `/api/ext/v1/reading/*` 下做一层薄代理，内部调用与主接口相同的服务函数，统一做作用域校验、请求体限制和日志脱敏。

## 5. 鉴权与安全设计

### 5.1 作用域扩展

当前：`library:read`、`library:write`。

新增：

| 作用域 | 允许的操作 |
|---|---|
| `reading:translate` | 划词 / 短文本翻译 |
| `reading:ask` | 针对已入库论文的问答 |
| `reading:annotate` | 读写批注与高亮 |
| `reading:meta` | 读取 blocks、guide、chat 历史 |

配对时由用户在设置页勾选（默认只给 `library:*`），脚本首次请求被拒时提示用户去设置页开启。

### 5.2 代理层规则

- 只接受已绑定 origin 的插件（或 origin 为空的用户脚本）。
- 校验 `paper_id` 必须存在于本地库，禁止任意路径。
- 翻译文本 ≤ 6000 字，问答问题 ≤ 2000 字，批注 quote ≤ 4000 字。
- 问答默认 `web=false`，需要联网检索时单独申请，避免脚本静默触发外部请求。
- 不透传 `config`：代理层从 `model-settings.json` 读取设置页选中的 answer 模型（翻译与问答共用），脚本不能指定模型或 base_url，也拿不到密钥。
- `papers/resolve` 只接受 DOI，只向白名单供应商查询；不提供"传入任意 URL 由服务端抓取"的能力，避免 SSRF。
- 日志只记插件 id 与 paper_id，不记正文。

### 5.3 主 token 不进入页面

- 任何情况下不把 `pr_token` 拼进 iframe URL 或页面 DOM。
- 若后续做 iframe，新增 `POST /api/ext/v1/reading/embed-ticket`：一次性、60 秒、只绑定单个 paper_id，换取阅读页的短时 cookie；ticket 用后即焚。
- 宿主页面的脚本即使读到 iframe src，也只能拿到已作废的 ticket。

### 5.4 威胁模型

| 威胁 | 现有防护 | 需要补的 |
|---|---|---|
| 恶意网页调用本地 API | 插件 Bearer + 配对等待窗口 | 保持 |
| 用户脚本 token 泄露 | 只存哈希、可撤销 | 作用域最小化 |
| 宿主页面读取 iframe 内容 | 同源策略 | ticket 一次性 |
| 脚本被篡改后滥读论文 | 作用域限制 | 敏感操作二次确认 |
| 本地端口被其他程序探测 | `/ping` 无敏感信息 | 保持 |

### 5.5 复用模型配置（不新建配置界面）

脚本侧不做模型设置，直接复用桌面端已有的「设置 → 模型服务」。

- **数据源**：`DATA_DIR/model-settings.json` 的 `settings.models` 与 `settings.active.answer`。前端保存模型配置时写的就是这份文件，桌面端与手机端已共用。
- **角色映射**：问答与划词翻译都使用 `active.answer` 选中的模型。当前前端只有 `answer` 与 `vision` 两种 `kind`，没有独立的翻译模型；将来新增翻译模型时，代理层按 `kind` 取即可，脚本无需改动。
- **服务端构造配置**：reading 代理读取 `baseUrl` / `apiKey` / `model`，在服务端构造 `LLMConfig` 后调用现有 `translate_text`、问答逻辑。请求体里的 `config` 一律忽略。
- **状态查询**：新增 `GET /api/ext/v1/reading/status`，只返回 `{ "model_ready": true, "model_label": "GPT-4o" }`，不返回密钥和 `base_url`。脚本面板据此显示「使用 Paperead 已配置模型」或「未配置模型」。
- **未配置时**：返回 400 与可读提示「请先在 Paperead 设置 → 模型服务 中配置可用模型」，脚本提供打开本地设置的入口。
- **兜底**：与现有 `_apply_llm_config` 保持一致，界面没有配置时可用 `PAPEREAD_API_KEY` 等环境变量；两者都没有才报错。
- **不采用前端现有做法**：前端调用翻译/问答时会把 `config`（含 API key）带进请求体。脚本若照搬，密钥会暴露在页面上下文里，与本地优先的定位冲突，因此明确禁止。

实现落点：把读取逻辑抽到 `server/model_config.py`，由 `ext_api` 与 `main` 共用，避免 `ext_api` 反向导入 `main` 造成循环依赖。

## 6. 页面锚定与批注同步

这是"结合阅读功能"的核心难点：外部网页的选区如何对应到本地论文的 block。

### 6.1 锚定流程

1. 取选区文本，做归一化：去多余空白、统一引号与连字符、去页眉页脚噪声。
2. 取前后各 60 字符作为上下文。
3. 在 `/api/papers/{id}/blocks` 的文本中做滑窗匹配：先精确匹配，失败再按相似度（如归一化编辑距离）找最佳候选。
4. 命中后写入批注：`block_id` + `quote` + 计算出的 `start/end`。
5. 未命中则不写批注，只保存为"自由笔记"，避免污染正文锚点。

### 6.2 单向优先

- 第一版只做"网页 → 本地"单向写入。
- 本地批注在网页上的回显不做，避免双向冲突与站点差异。
- 同一论文同一 quote 的重复写入要幂等，避免用户反复点击产生重复批注。

### 6.3 站点差异

- arXiv HTML 版、PMC、出版商 HTML 结构差异大，锚定成功率需要用真实样本评估。
- PDF 页面无法注入，只能引导用户打开本地阅读页。

## 7. 交互设计

- **悬浮球**：仅在识别到论文特征时出现，点击展开面板。
- **选区工具条**：选中 ≥ 8 个字符后出现，按钮为「翻译 / 提问 / 高亮 / 复制引用」。
- **面板内容**：标题、作者、年份、DOI、本地状态（未入库 / 已入库 / 解析中）、目标语言、保存与打开阅读页。
- **快捷键**：`Alt+T` 翻译选区、`Alt+Q` 提问、`Alt+S` 保存（页面内绑定，避免与站点冲突）。
- **反馈**：所有失败都要有中文原因，区分「服务未启动」「未配对」「无 PDF 权限」「解析失败」。

## 8. 风险与未证实项

| 项 | 状态 | 影响 | 应对 |
|---|---|---|---|
| Chrome LNA 是否拦截 GM 请求 | 未证实 | 可能导致脚本完全无法访问本地服务 | Chrome 142+ 真机验证；失败则改用扩展 |
| ScriptCat 的 iframe 注入能力 | 未证实 | 影响侧栏方案 | 先做新标签页 |
| 严格 CSP 站点 UI 注入 | 部分证实 | 面板可能不显示 | `GM_addElement` 降级 + 纯文本模式 |
| 锚定准确率 | 未验证 | 批注错位 | 先用 arXiv/PMC 各 20 篇评估 |
| 市场审核周期 | 已知风险 | 更新不及时 | 内测用本地文件安装，正式版走市场 |
| 用户脚本在 Chrome 138+ 的开关 | 已知 | 安装成功率下降 | 引导页明确说明 |
| PDF 查看器不可注入 | 确定 | 无法在 PDF 页划词 | 引导打开本地阅读页 |

## 9. 实施路线

### P0：页面内快操作（建议先做）

1. **服务端新增 `/api/ext/v1/reading/*` 代理与作用域校验**，内部转发到现有翻译、问答、批注服务函数；同时新增 `GET /reading/status` 与 `server/model_config.py`，模型取自设置页 active answer，脚本不传 `config`。
2. **设置页增加阅读作用域开关**（`reading:translate` / `reading:ask` / `reading:annotate` / `reading:meta`），默认关闭，配对后按需开启。
3. **新增站点适配器注册表**：
   - 通用适配器：Highwire `citation_*` → JSON-LD → OpenGraph。
   - 站点适配器：arXiv（abs / html / pdf 三种 URL）、PubMed、PMC。
   - 按 URL 正则路由到适配器，适配器只负责补字段，失败回退通用逻辑。
4. **HTML 优先注入**：识别 `arxiv.org/html/*`、`ar5iv.org/*`、PMC HTML 正文，命中后直接在页面内启用划词工具条；摘要页与 PDF 页降级为「打开本地阅读页」。
5. **DOI 开放全文解析**：新增 `GET /api/ext/v1/papers/resolve?doi=`，服务端只向白名单供应商（OpenAlex / Unpaywall / Crossref）查询，返回 `oa_url`、`license`、`source`；不接受用户传入任意 URL 让服务端抓取，避免 SSRF。
6. **脚本增加选区工具条**：翻译（`/reading/translate`）、复制引用（本地拼接）、高亮（`/reading/annotations`）。
7. **脚本增加「打开阅读页」按钮**，携带 `paper_id` 与可选定位参数，复用现有阅读页。
8. **真机验证**：arXiv HTML、ar5iv、PMC HTML 各 3 篇，检查 LNA、CSP 注入、划词翻译与入库全链路。

验收：
- 在 arXiv HTML / ar5iv / PMC HTML 页划词能出译文。
- 只给出 DOI 的出版商页面，能解析到 OA PDF 并入库，解析失败时给出明确原因。
- 未入库论文能一键入库并打开阅读页；已入库论文直接命中。
- 新增一个站点适配器不需要改核心代码。

### P1：选中问答与批注写入

1. 代理 `/ask`（非流式）与 `/annotations`。
2. 选区锚定到 blocks，失败降级为自由笔记。
3. 面板显示问答结果与来源。
4. 幂等与重复批注处理。

验收：选中一段正文提问能返回带出处的答案；高亮能出现在本地阅读页。

### P2：可选侧栏阅读

1. 新增一次性 embed ticket。
2. 页面右侧可折叠 iframe，加载本地阅读页。
3. LNA 授权引导与降级到新标签页。
4. 只在用户主动开启时启用，默认关闭。

验收：在 arXiv 页右侧可直接读本地解析版，不丢失登录态。

## 10. 竞品实现调研

### 10.1 三种实现流派

| 流派 | 代表 | 注入方式 | 优点 | 代价 |
|---|---|---|---|---|
| 常驻全站注入 | 沉浸式翻译、Zotero Connector、Unpaywall | content script 常驻（`document_start`、`all_frames`） | 任意页面即用，交互一致 | 权限极广，安装时提示吓人，站点兼容成本高 |
| 按需注入 | DeepL 扩展、SciSpace Copilot | `activeTab` + `scripting`，点击才注入 | 权限最小，隐私叙事好 | 每次需要一次点击 |
| 独立阅读器 | 知云、S2 Reader、Readwise、Paper Digest、Consensus | 不注入，上传或重渲染 PDF | PDF 完全可控，批注锚点稳定 | 需要用户离开原页面，或先上传 |

**对我们的含义**：Paperead 的脚本版目前是「常驻但只在论文页显示悬浮球」，接近第一派；如果要降低安装时的权限提示，可以改成 `activeTab` 按需注入，与 DeepL 一致。

### 10.2 逐产品要点

| 产品 | 形态 | PDF 处理 | 问答上下文 | 可借鉴 |
|---|---|---|---|---|
| Scholarcy | 扩展 + 上传 | 服务端抓取，仅 OA/HTML | 抽取式摘要，无对话 | 明确声明能力边界，不承诺所有 PDF |
| SciSpace | Web 阅读器 + 侧栏 | 原生 PDF 用截图/视觉（Snip） | 选中片段 + 当前页 | HTML 划词 + PDF 截图兜底 |
| Consensus | Web 应用 | 云端上传，20 篇/次 | 上传全文 + 检索库 | 多篇全文对比问答的产品定义 |
| Elicit | Web + 取全文扩展 | 云端解析 | 自建 1.38 亿摘要库 + 全文 | **扩展只负责取全文，解析问答在后端** |
| Semantic Scholar | 扩展 + Web Reader | React-PDF 重渲染 + PaperMage 解析 | 全文 + 引文卡片 | 坐标级锚点、开源解析栈 |
| Paper Digest | Web 阅读器 | 上传/链接解析 | 整篇文档 | 阅读器与发现流绑定 |
| Sider / Monica / MaxAI | 侧栏 + 浮层 | 上传或抓 URL，长文截断 | 当前页文本 | 侧栏交互与自动上下文 |
| 沉浸式翻译 | 常驻注入 | 独立阅读页 + canvas 叠译文框 | 无问答 | 段落级双语插入、DOM 监听 |
| 知云 | Windows 阅读器 | 自有渲染 | 无 | 划词后侧栏译文，低打断 |
| DeepL | 按需注入 | 文件上传 | 无 | 最小权限 + 按需注入 |
| Zotero Connector | 常驻注入 + 后台 | 不处理 PDF 正文 | 无 | **translator 注册表 + 多级回退** |
| Readwise Reader | 扩展只保存 + Web Reader | 自有副本 | 无 | **DOM 区间 + 字符偏移双锚点** |
| Unpaywall | 常驻注入 | 无 | 无 | 扫 DOI → OA API → 注入标签 |

（Open Access Button 已于 2025-11-18 停服，官方建议改用 Unpaywall。）

### 10.3 PDF 是共同的分水岭

所有产品的结论一致：**原生 PDF 查看器无法注入**。

- Chrome 内置查看器是独立扩展页面，用户脚本和扩展都读不到文本层与选区。
- 社区流传的 `pdf_viewer` postMessage 技巧属非官方行为，脆弱，不应用于生产。
- 可行替代只有四条：自建 pdf.js 重渲染（S2、沉浸式翻译 Pro、Paperead 已有）、截图 + 视觉模型（SciSpace）、服务端抓取解析（Scholarcy、Elicit）、桌面阅读器（知云）。

Paperead 已经具备 pdf.js 阅读页与解析链路，**这是相对脚本猫方案的结构性优势**，不应为了"页面内"体验放弃它。

### 10.4 元数据与锚定的成熟做法

**元数据：Zotero translator 是行业范本。**

- 按 URL 正则初筛，再执行 `detectWeb` / `doWeb`。
- 优先 Highwire `citation_*`，回退 DOI → CrossRef，再回退站点专用解析。
- translator 沙箱运行、可远程更新，新增站点不用发版。

我们当前只有一套通用提取逻辑，建议后续做成「站点适配器注册表」：通用提取兜底，arXiv / PubMed / PMC / 主要出版商各一个小适配器。

**锚定：两个成熟范式。**

- Zotero：PDF 用页码 + 归一化矩形；EPUB 用 CFI；文档改版会错位。
- Readwise：规范化 HTML 的序列化 DOM 区间 `<start>,<end>` + 纯文本字符偏移；新建高亮必须在父文档匹配成功，否则拒绝。
- 共同点：**自己保存一份规范化副本**，锚点多字段冗余，匹配失败宁可拒绝也不写错位置。

### 10.5 对我们的五点结论

1. **HTML 优先，PDF 兜底**：arXiv HTML / ar5iv / PMC HTML 上做页内注入与锚定，成功率远高于解析 PDF；PDF 场景引导到本地阅读页。
2. **脚本只做取数与快操作，解析和问答留在本地服务**：这与 Elicit 的分工一致，也符合我们"本地优先、不上传 PDF"的定位。
3. **元数据引入适配器注册表**：`citation_*` → DOI → 站点适配器，避免每加一个站点就改核心代码。
4. **批注锚点做多字段冗余**：`quote` + 前后文 + 字符偏移 + `block_id` + `page/rect` + 内容哈希，逐级降级，匹配失败转自由笔记。
5. **权限按需申请**：参考 DeepL 的 `activeTab`，把"常驻全站"改成"点击后注入"，降低安装阻力；这与脚本猫缺少细粒度权限的现实也匹配。

### 10.6 差异化空间

竞品普遍是云端上传 + 登录 + 额度制；本地优先、无需登录、PDF 不出本机是 Paperead 的明确差异点。反过来，竞品的站点适配器数量和解析质量是长期积累，短期无法追平，应聚焦 arXiv / PubMed / PMC 三个高频场景做深。

## 11. PDF 接管方案（已选定）

### 11.1 决策

用户选定「只做接管 PDF」：扩展把 `.pdf` 导航重定向到自带的 pdf.js 阅读页，在原标签页内完成选词翻译与提问。**脚本猫版无法实现**，因为用户脚本不能接管导航，也不能在 Chrome 内置 PDF 查看器里运行。

### 11.2 链路

```text
点击/跳转到 .pdf
  │
  ├─ background: webNavigation.onBeforeNavigate（main_frame）
  │    └─ storage.pdfTakeover !== false → tabs.update 到 viewer.html?file=<encoded>
  │
  ├─ viewer（扩展页，chrome-extension://<id>）
  │    ├─ 首次请求该站点 optional host 权限（用户点一次「允许读取」）
  │    ├─ fetch(file, credentials: include) 复用站点登录态
  │    ├─ 校验 %PDF- 魔数
  │    ├─ pdf.js 渲染 canvas + TextLayer（可选中）
  │    └─ 选区工具条：翻译 / 提问
  │
  ├─ 后台入库：ingest(原 URL) → 解析轮询 → paper_id
  │
  └─ /api/ext/v1/reading/translate、/reading/ask
       └─ 服务端读取 model-settings.json 的 answer 模型，扩展不持有密钥
```

### 11.3 已实现

- 服务端：`/api/ext/v1/reading/status`、`/reading/translate`、`/reading/ask`；作用域校验；默认配对作用域扩展为 `library:* + reading:*`。
- 扩展：`webNavigation` 权限、PDF 导航接管、`viewer.html` 阅读页、pdf.js 渲染与文本层、选区工具条、翻译弹层、入库与解析轮询、提问、关闭/恢复接管。
- 测试：`tests/test_ext_reading.py`（状态、翻译、问答、作用域、未授权），共 28 项后端测试通过；扩展 4 项单元测试通过。

### 11.4 已知限制

| 限制 | 说明 | 后续 |
|---|---|---|
| 首次安装权限提示 | 安装时显示「读取和更改所有网站数据」 | 商店审核更严格；可回退 optional 权限 |
| 扫描版 PDF 无文本层 | pdf.js 只能选区已有文本层 | P2 接 OCR 或视觉模型 |
| 大文件渲染 | 逐页懒加载，首屏只渲染前 3 页 | 需要可加缩略图与页码跳转 |
| 原生 PDF 查看器 | 关闭接管后仍无法注入 | 符合预期 |
| 重新配对 | 旧 token 不含 reading 作用域 | 撤销后重新配对一次 |

### 11.5 零操作配置

- **后台自动配对**：扩展用 `alarms` 每分钟检查一次是否已配对，未配对就静默尝试空码认领；没有等待窗口时请求返回 410，不产生副作用。
- **桌面端自动开窗**：Paperead 前端启动时若没有任何已连接插件，会自动开一个 120 秒等待窗口；打开「插件与连接」设置页时也会自动续开。
- **安装即授权**：`host_permissions` 改为 `https://*/*` 与 `http://*/*`，PDF 首次打开不再逐站点申请权限。
- **用户唯一动作**：浏览器强制的「添加扩展」。配对、站点授权、PDF 接管全部自动完成。
- **代价**：商店安装提示会显示「读取和更改所有网站上的数据」，审核更严格。若后续要降低权限提示，可回退为 `optional_host_permissions`，并接受首次打开站点时的一次授权。

## 12. 参考来源


- ScriptCat API 文档：https://docs.scriptcat.org/docs/dev/api
- ScriptCat 元数据与 `@inject-into`：https://docs.scriptcat.org/docs/dev/meta/
- ScriptCat 常见问题（CSP / Trusted Types）：https://docs.scriptcat.org/docs/use/QA/
- 脚本站服务协议与审查规则：https://scriptcat.org/zh-CN/terms-of-service
- Chrome Local Network Access：https://developer.chrome.com/blog/local-network-access
- Chrome 用户脚本变更：https://developer.chrome.com/blog/chrome-userscript
- 本地代码：`server/main.py`、`server/ext_api.py`、`server/plugin_auth.py`、`web/src/components/ReaderView.tsx`、`web/src/components/Reader.tsx`
- Zotero Connectors：https://github.com/zotero/zotero-connectors
- Zotero Translator 文档：https://www.zotero.org/support/dev/translators
- Zotero Embedded Metadata translator：https://github.com/zotero/translators/blob/master/Embedded%20Metadata.js
- Readwise Reader API（高亮锚点字段）：https://readwise.io/reader_api
- Unpaywall 扩展源码：https://github.com/ourresearch/unpaywall-extension
- 沉浸式翻译（已闭源）与旧版开源实现：https://github.com/immersive-translate/immersive-translate
- BabelDOC（沉浸式翻译 PDF 排版，AGPL-3.0）：https://github.com/funstory-ai/BabelDOC
- Semantic Reader / PaperCraft：https://openreader.semanticscholar.org/PaperCraft
- SciSpace Copilot 帮助：https://scispace.com/help/en/articles/10775065-scispace-extension-how-to-install-and-use-it
- Elicit 浏览器扩展（只取全文的分工）：https://support.elicit.com/en/articles/14759113-elicit-browser-extension
- Scholarcy FAQ（OA/PDF 能力边界）：https://www.scholarcy.com/faq/
- ar5iv：https://ar5iv.org/ ｜ arXiv HTML：https://info.arxiv.org/about/accessible_HTML.html
