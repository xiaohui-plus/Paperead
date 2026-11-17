# Paperead 浏览器插件与外部阅读器接入设计

- 日期：2026-11-17
- 状态：待评审
- 范围：第一版浏览器扩展（采集入库）、服务端插件 API、桌面端配对入口；外部 PDF 阅读器接入只定方向，不进入第一版实现

## 1. 目标

把用户已经在浏览器里找到的论文，以最低摩擦送进 Paperead，然后回到现有阅读页完成翻译、问答、引用和批注。

第一版成功的标准：

1. 在 arXiv、PubMed、PMC、DOI 落地页和常见出版商页面，一键把论文和 PDF 存入 Paperead。
2. 元数据（标题、作者、年份、期刊、DOI、摘要）正确落库，不需要用户二次整理。
3. 同一篇论文重复保存不产生副本，已入库的论文能直接打开。
4. 应用未启动、PDF 有登录墙、解析失败时，用户能看懂发生了什么，并且有下一步操作。

## 2. 非目标

- 不在浏览器里重建阅读器、翻译或问答界面。
- 不做 Paperead 应用内的第三方插件运行时（Obsidian 式插件生态）。
- 第一版不做 Adobe Acrobat 原生插件。
- 不允许服务端替扩展抓取任意 URL。
- 不引入账号、云端同步或计费能力。

## 3. 现状与约束

| 事实 | 影响 |
|---|---|
| 桌面端已有 FastAPI 服务，端口优先 17321，占用时顺延 17321–17330 | 扩展可发现并复用该服务，不必新起进程 |
| `/api` 统一由 token + Origin 白名单保护，见 `server/main.py` 的 `guard` | 扩展默认会被 403，需要独立命名空间 |
| token 以 HttpOnly cookie 下发给本机页面 | 扩展读不到，需要配对换独立 token |
| 上传、查重、解析链路已存在，见 `server/main.py` 的 `/api/papers/upload` | 入库逻辑可直接复用 |
| `store.add` 会把 meta 合并进论文记录，见 `server/store.py` | 无需新增数据表 |
| 学术检索导入已定义元数据字段，见 `server/scholar_api.py` | 插件沿用同一套字段 |
| 阅读页是 Web 路由 `/?p=<paper_id>`，见 `web/src/App.tsx` | 保存后直接打开该地址即可 |
| 仓库协议为 AGPL-3.0-only | 扩展若复用本项目代码或以衍生作品分发，需遵循 AGPL；商店发布时附源码链接 |

## 4. 技术路线

采用 **本地 HTTP API + 跨浏览器 MV3 扩展**。

- 桌面端是 Hub，负责存储、解析、翻译、问答和阅读。
- 扩展是采集器，只负责提取元数据、取得 PDF 字节、调用本地 API、反馈状态。
- 阅读仍回到现有 Web 阅读页，不复制阅读能力。

放弃的备选：

- **Native Messaging**：鉴权更强，但每个浏览器都要注册原生消息宿主，Windows 安装与跨平台打包成本明显更高；后续侧栏流式仍要回到 HTTP。
- **只传 URL 由服务端抓取**：登录墙和反爬场景基本不可用，还引入 SSRF 风险，只作为开放获取的兜底。

## 5. 总体架构

```text
arXiv / PubMed / PMC / DOI / 出版商页面 / Google Scholar
                    │
                    │ 元数据 + PDF bytes
                    ▼
        浏览器扩展（WXT + React + TypeScript）
                    │
                    │ /api/ext/v1  Authorization: Bearer <plugin token>
                    ▼
        Paperead 本地服务（127.0.0.1:17321）
          ├─ 插件鉴权与作用域
          ├─ 查重：source_id → DOI → pdf_sha256
          ├─ store.add(meta) 落盘
          ├─ _parse_task 后台解析
          └─ 状态查询
                    │
                    ▼
        http://127.0.0.1:17321/?p=<paper_id>
             现有阅读页（翻译 / 问答 / 引用 / 批注）
```

## 6. 服务端设计

### 6.1 命名空间

新增 `server/ext_api.py`，挂载在 `/api/ext/v1`。现有 `/api` 的行为不变。

插件 token 只能访问该命名空间，不能访问模型配置、研究 agent、终端、文件系统等接口。

### 6.2 接口契约

#### `GET /api/ext/v1/ping`

无需 token，用于端口发现和运行状态检测。

```json
{
  "ok": true,
  "app": "paperead",
  "api": 1,
  "version": "0.2.20"
}
```

#### `POST /api/ext/v1/pair/session`

需要主应用登录态（现有 cookie，仍受主 token 保护）。桌面端设置页调用，生成一次性配对码。

请求：

```json
{ "name": "Chrome" }
```

响应：

```json
{ "code": "482913", "expires_at": 1794904600 }
```

规则：

- 6 位数字，120 秒有效，单次使用。
- 同一时间只保留最新一个配对码。
- 配对码只存内存，不落盘。

#### `POST /api/ext/v1/pair`

无需主 token，是唯一对扩展开放的无鉴权写接口。

请求：

```json
{
  "code": "482913",
  "name": "Chrome 扩展"
}
```

响应：

```json
{
  "token": "<随机 32 字节 hex>",
  "api": 1,
  "scopes": ["library:read", "library:write"]
}
```

规则：

- 校验配对码、有效期、是否已使用。
- 扩展身份以请求 `Origin` 为准，服务端从 `chrome-extension://` 或 `moz-extension://` 中解析并绑定；不信任请求体里自报的 ID。
- 非扩展 Origin（包括普通网页）调用一律 403，避免网页脚本借本机接口配对。
- token 只返回一次，服务端只保存 SHA-256。
- 按 IP 限流，每分钟最多 5 次失败尝试。
- 配对码错误返回 401，过期返回 410，限流返回 429。

#### `GET /api/ext/v1/papers/lookup`

需要 `library:read`。

查询参数：`source_id`、`doi`、`url` 至少一个。

响应：

```json
{
  "found": true,
  "paper": {
    "id": "p_ab12cd34ef56",
    "title": "…",
    "status": "ready",
    "url": "http://127.0.0.1:17321/?p=p_ab12cd34ef56"
  }
}
```

查重顺序：`source_id` → `doi` → 规范化后的 `url`。URL 规范化规则：去掉 fragment、`utm_*` 等跟踪参数、末尾斜杠，主机名小写；其余部分保持原样。

#### `POST /api/ext/v1/papers/ingest`

需要 `library:write`，`multipart/form-data`。

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | PDF 文件 | 是 | 最大 100MB，需以 `%PDF-` 开头 |
| `metadata` | JSON 字符串 | 是 | 见下表 |
| `folder_id` | 字符串 | 否 | 目标文件夹 |

`metadata` 字段沿用学术检索导入：

```json
{
  "source": "arxiv",
  "source_id": "arxiv:2401.00001",
  "title": "…",
  "authors": "A, B, C",
  "abstract": "…",
  "doi": "10.…",
  "url": "https://arxiv.org/abs/2401.00001",
  "year": 2024,
  "venue": "…"
}
```

行为：

1. 校验 PDF 魔数、大小、文件夹是否存在。
2. 先按 `source_id` 查全局库；再按 `pdf_sha256` 在目标文件夹查重。
3. 已存在则复用记录；状态不是 `ready` 时重新触发解析。
4. 不存在则 `store.add(filename, data, meta)`，并异步触发 `_parse_task`。
5. 返回论文公开字段与 `already` 标记。

响应：

```json
{
  "paper": { "id": "p_…", "title": "…", "status": "uploaded" },
  "already": false
}
```

#### `GET /api/ext/v1/papers/{paper_id}`

需要 `library:read`。返回论文状态、页数、错误原因和可打开的阅读地址。扩展用 1.5 秒间隔轮询，直到 `ready` 或 `failed`。

#### `GET /api/ext/v1/folders`

需要 `library:read`。返回 `[{ "id": "…", "name": "…", "count": 3 }]`，供扩展选择归档位置。

#### `GET /api/ext/v1/admin/plugins`

需要主应用登录态，供设置页展示已连接插件。返回插件 id、名称、作用域、创建时间和最近使用时间，不含 token 哈希。

#### `DELETE /api/ext/v1/admin/plugins/{id}`

需要主应用登录态。撤销指定插件，返回 `{ "ok": true }`；不存在的 id 返回 404。

#### 暂不实现

`/events` SSE 放到第二阶段。第一版用轮询，避免 MV3 service worker 对 `EventSource` 的兼容问题。

### 6.3 鉴权与存储

插件 token 存放于配置目录 `config_dir()/plugins.json`，不随文献库迁移：

```json
{
  "version": 1,
  "plugins": [
    {
      "id": "pl_…",
      "name": "Chrome 扩展",
      "origin": "chrome-extension://<id>",
      "token_sha256": "…",
      "scopes": ["library:read", "library:write"],
      "created_at": 1794900000,
      "last_used_at": 1794900000
    }
  ]
}
```

- 原子写入，避免半截文件。
- 设置页展示名称、最近使用时间、作用域，并支持撤销。
- 撤销后该 token 立即失效，扩展收到 401 后清除本地 token 并要求重新配对。
- token 不写日志；请求日志只记录插件 id。

### 6.4 Origin / CORS 改造

只对 `/api/ext/v1` 放开扩展 Origin，主 `/api` 保持原样。

`guard` 分支规则：

1. 精确匹配 `/api/ext/v1/ping` 和 `/api/ext/v1/pair`：不校验主 token，其余路径不享受豁免。
2. `/api/ext/v1/pair/session`、`/api/ext/v1/admin/*`：仍按主应用规则校验 cookie / 主 token。
3. `/api/ext/v1/papers/*`、`/api/ext/v1/folders`：校验 `Authorization: Bearer <plugin token>`；请求带 Origin 时必须与 token 绑定的扩展 Origin 一致，不带 Origin 时以 token 为准。
4. `/pair` 只接受 `chrome-extension://` 或 `moz-extension://` Origin；普通网页 Origin 和无 Origin 请求一律 403。`/ping` 接受任意本机请求，方便端口探测。
5. 对扩展路由动态返回 `Access-Control-Allow-Origin`、`Access-Control-Allow-Headers: authorization, content-type`、`Access-Control-Allow-Methods`，并处理 `OPTIONS` 预检。

Chrome 扩展请求的 Origin 可能是 `chrome-extension://<id>`，也可能因请求类型被省略；两种都要覆盖。

### 6.5 端口发现

扩展按顺序探测 `17321` 到 `17330` 的 `/ping`，单次超时 300ms，成功后缓存端口。

全部失败时提示用户启动 Paperead；仍失败则允许手动填写端口。桌面端回退到随机端口的情况不保证自动发现。

## 7. 扩展设计

### 7.1 技术选型

- WXT + React + TypeScript，一套代码产出 Chrome/Edge MV3 与 Firefox MV3。
- 仓库位置：`extensions/browser/`，独立 `package.json`。
- 第一版不抽 `packages/plugin-sdk`。只有一个消费者时，抽象没有收益；等第二个插件出现再提取。

### 7.2 目录结构

```text
extensions/browser/
  package.json
  wxt.config.ts
  entrypoints/
    background.ts
    popup/
      index.html
      main.tsx
      App.tsx
  lib/
    api.ts          # ping / pair / lookup / ingest / status
    metadata.ts     # citation_* / JSON-LD / URL 规则，按需注入
    pdf.ts          # PDF 获取降级链路
    types.ts        # 元数据、任务与论文摘要类型
  tests/
    metadata.test.ts
```

不设常驻 content script：元数据在用户点击时通过 `scripting.executeScript` 注入，避免安装时申请全站权限。

### 7.3 权限

| 权限 | 用途 |
|---|---|
| `storage` | 保存 token、端口、上次选择的文件夹 |
| `activeTab` | 读取当前页信息 |
| `scripting` | 按需注入元数据提取脚本 |
| `contextMenus` | 右键保存 |
| `notifications` | 解析完成 / 失败提示 |
| `host_permissions: http://127.0.0.1/*` | 访问本地 API |
| `optional_host_permissions: https://*/*, http://*/*` | 首次保存某站点时按需申请，降低安装时的权限提示 |

不使用 `cookies` 权限；登录态通过页面上下文请求或 `credentials: include` 复用。

### 7.4 第一版交互

1. **工具栏弹窗**：展示识别到的标题、作者、年份、DOI、PDF 来源；选择文件夹；「保存到 Paperead」。
2. **右键菜单**：「保存当前页面」「保存链接指向的 PDF」。
3. **PDF 标签页**：检测 `.pdf` 标签页，在弹窗提供「保存到 Paperead」和「用 Paperead 打开」。Chrome 内置 PDF 查看器无法注入内容脚本，因此不做页内悬浮按钮。
4. **状态反馈**：保存后显示「解析中」，轮询状态；完成后系统通知 + 「打开阅读页」。
5. **已入库识别**：弹窗打开时先调用 `lookup`，已存在则直接显示「已在库中」和打开按钮。

第一版不做：Scholar 列表批量注入、页内悬浮按钮、侧栏问答、划词翻译。这些放到后续阶段。

### 7.5 元数据提取

按优先级：

1. Highwire `citation_*` 元标签。
2. JSON-LD `ScholarlyArticle`。
3. OpenGraph `og:title` / `og:description`。
4. 页面 `<title>`。

标识符与 PDF 地址：

- DOI：`10.\d{4,9}/…` 正则，优先 `citation_doi`。
- arXiv：`/abs/<id>`、`/pdf/<id>`，规范化为 `arxiv:<id>`。
- PubMed：`/pubmed/<pmid>`。
- PMC：`/articles/PMC<id>`。
- PDF：`citation_pdf_url` → `<link type="application/pdf">` → arXiv `/pdf/<id>` → 页面内 `.pdf` 链接。

作者统一用 `, ` 连接，年份取 `citation_publication_date` 前四位，期刊取 `citation_journal_title` 或 `citation_conference_title`，与现有检索导入保持一致。

### 7.6 PDF 获取降级链路

1. 开放获取直链：后台 `fetch`，带站点 host 权限。
2. 同源 PDF：在页面上下文 `fetch`，天然携带出版商登录态。
3. 跨域但需要登录态：后台 `fetch` 使用 `credentials: include`。
4. 返回内容不是 PDF（登录页、HTML 拦截页、验证码页）：不保存，提示「该 PDF 需要登录或验证，请下载后拖入 Paperead」。
5. 用户手动下载后拖入桌面端，仍走现有上传链路。

上传前校验 `%PDF-` 魔数和大小。任何一步失败都要明确告诉用户失败原因，不允许静默失败或标记成功。

MV3 的 service worker 可能被浏览器回收，大文件上传和 PDF 流式读取放在 offscreen document 或扩展页面里执行，不通过 `sendMessage` 传大块 ArrayBuffer。上传前保留任务状态，失败或中断时能从上次进度重试。

## 8. 入库时序

```text
用户点击保存
  │
  ├─ 扩展提取 metadata + 候选 PDF 地址
  ├─ GET /papers/lookup
  │    └─ 命中：展示「已在库中」，结束
  ├─ 按降级链路取得 PDF 字节
  ├─ POST /papers/ingest（file + metadata + folder_id）
  │    ├─ 服务端查重
  │    ├─ store.add + _parse_task
  │    └─ 返回 paper_id / status
  ├─ 轮询 GET /papers/{id}
  │    ├─ ready：通知 + 打开 http://127.0.0.1:<port>/?p=<paper_id>
  │    └─ failed：通知失败原因
  └─ 应用未运行：提示启动，不丢当前页面数据
```

## 9. 桌面端改动

设置页新增「插件与连接」面板，放在现有 `SettingsDialog` 中：

- 安装入口：Chrome / Edge（P1）、Firefox（P2），附源码仓库链接。
- 「生成配对码」按钮与倒计时。
- 已连接插件列表：名称、作用域、最近使用时间、撤销。
- 未运行时给扩展的提示文案示例。
- 说明插件只能访问文献库读写，不能访问模型配置。

第一版不在应用内做论文入库 Toast；通知由扩展自己完成，避免引入 Tauri 事件桥。

## 10. 错误处理与边界

| 场景 | 行为 |
|---|---|
| 应用未启动 | `/ping` 失败，弹窗提示启动 Paperead，保留识别结果 |
| token 失效 / 被撤销 | 收到 401，清除本地 token，提示重新配对 |
| 配对码错误 / 过期 | 分别提示「配对码不正确」「配对码已过期，请重新生成」 |
| 论文重复 | 按 `source_id` / `pdf_sha256` 复用已有记录，不新建副本 |
| PDF 是登录页 | 校验魔数失败，提示下载后拖入 |
| PDF 超过 100MB | 本地拦截，提示压缩或手动导入 |
| 解析失败 | 展示服务端错误原因，提供「重试解析」 |
| 文件名异常 | 服务端清洗文件名，拒绝路径穿越 |
| 端口被占用 | 扩展探测 17321–17330；仍失败则手动填写 |
| 多浏览器 / 多配置文件 | 每个扩展安装实例独立配对、独立 token |
| Firefox MV3 差异 | 通过 WXT 构建，补充 `browser_specific_settings.gecko.id` |

## 11. 测试

服务端：

- 配对码：有效期、单次使用、错误码、限流。
- 鉴权：无 token、错误 token、撤销后 token、跨作用域访问。
- Origin：允许扩展 Origin，拒绝未配对 Origin，预检正确。
- 入库：元数据落库、重复 source_id、重复 pdf_sha256、非 PDF、超大文件、文件夹不存在。
- 状态：解析中 → ready，解析失败返回原因。

扩展：

- 元数据提取单元测试，使用 arXiv、PubMed、PMC、出版商页面的 HTML 快照。
- 手工验收：arXiv 入库、PubMed 入库、重复保存、应用未启动、登录墙 PDF、撤销 token 后重配对。
- 可选：Playwright 持久化上下文加载未打包扩展，覆盖保存主链路。

## 12. 里程碑

| 阶段 | 交付 | 验收 |
|---|---|---|
| P0 | `/api/ext/v1`、配对、插件存储、设置页面板 | 用 curl（`Origin: chrome-extension://test`）完成配对、入库、查重、状态轮询；撤销后立即 401 |
| P1 | Chrome/Edge 扩展：弹窗、右键、PDF 动作、通知 | arXiv 一键入库；标题/年份/页码正确；重复保存无副本 |
| P2 | Firefox、Scholar 批量保存、离线队列 | 断网任务不丢，恢复后自动重试 |
| P3 | 侧栏问答、划词提问 | 不离开网页完成单篇论文问答 |
| P4 | Zotero 插件、`paperead://` 深链、Windows「打开方式」 | 从 Zotero / 资源管理器直接打开 Paperead 阅读页 |

## 13. 外部 PDF 阅读器接入方向

不做 Adobe Acrobat 原生插件。优先顺序：

1. **Zotero 插件**：复用现有双向同步，增加「在 Paperead 中打开」和高亮/批注互通。用户重合度最高，边际成本最低。
2. **Windows「打开方式」**：安装包注册 PDF 关联入口，调用桌面端导入接口。
3. **`paperead://` 深链**：需要 Tauri deep-link 插件与 NSIS 协议注册，放到 P4。
4. **浏览器内置 PDF 查看器**：由浏览器扩展的页面动作覆盖，不单独开发。

## 14. 默认决策

以下作为第一版默认值，实施前可调整：

1. 首发只发 Chrome/Edge，Firefox 放 P2。
2. 登录墙 PDF 尽力而为，失败时引导用户下载后拖入，不做绕过付费墙的尝试。
3. 不在应用内做入库 Toast，只由扩展发系统通知。
4. 扩展名称使用「Paperead Connector」，商店描述强调「把网页上的论文送进本地 Paperead 文献库」。
