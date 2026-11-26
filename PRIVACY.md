# Privacy Policy

**Project:** Paperead
**Applies to:** Paperead desktop application (Windows) and Android application
**Last updated:** 2026-11-26

Paperead is an open-source application. This policy describes what data the
application handles, where it is stored, and when it leaves your device.

## Summary

- No account is required, and there is no telemetry, analytics, crash
  reporting, or advertising identifier.
- Your documents, notes, highlights, library, and settings are stored on your
  own device.
- Data leaves your device only when you use a feature that requires it: calling
  the language model you configured, searching scholarly databases, syncing
  with your Zotero account, checking for updates, or submitting feedback.
- Feedback you submit is published publicly in a Gitee repository.

## Data stored on your device

Paperead stores the following locally, in the application data directory
(`%LOCALAPPDATA%\Paperead\data` on Windows, or the app's private storage on
Android):

- Imported PDF files and files you create in projects
- The literature library database (titles, authors, metadata, reading state)
- Notes, highlights, comments, translations, and reading progress
- Application settings, including the model endpoint, API key, and model name
  you enter

This data is not uploaded by the application. It remains on your device until
you delete it or uninstall the application. Deleting the data directory removes
all of it.

Model credentials are stored locally in the application data directory and in
the application's local browser storage. They are not encrypted at rest by
Paperead and are never sent anywhere except to the model endpoint you
configured, as an authorization header.

## Data sent to third parties

Paperead contacts external services only for the functions you invoke:

| Function | What is sent | Destination |
| --- | --- | --- |
| Reading, translation, question answering, research assistant | The text you ask about (for example selected paragraphs or document pages), your prompts, and the conversation history | The language model endpoint you configure. This can be a public provider, a self-hosted service, or a local model server |
| Literature search | Your search terms and filters | arXiv, PubMed, OpenAlex, Europe PMC, Semantic Scholar, CORE |
| Downloading open-access PDFs | The paper URL or DOI | The publisher or repository hosting the file |
| Zotero synchronisation | The library metadata required for the sync | Your own Zotero account (`api.zotero.org`), using the API key you provide |
| Update check and runtime downloads | Standard HTTP request metadata (IP address, user agent) | `gitee.com`, `github.com`, `ghproxy.net` |
| Reading-notes catalogue | Standard HTTP request metadata | `gitee.com`, `github.com`, `raw.githubusercontent.com` |
| Feedback (optional, user initiated) | The title, description, and screenshots you attach | A public Gitee repository, see below |

The language model provider processes your content under its own privacy
policy. If you use an external provider, the text you send to it is subject to
that provider's terms. If you use a local or self-hosted model, the content
never leaves your machine.

### Feedback submissions are public

Submitting feedback is optional and only happens when you click the submit
button. When you do, the title, description, and any screenshots you attach are
written to a public repository
(`https://gitee.com/deng_xiaohui/paperead_issue`) as a public issue, where
anyone can read them. Do not include personal or confidential information in
feedback. To have a submission removed, open an issue in the main repository.

## What is not collected

- No usage statistics, event tracking, or session recording
- No crash reports or diagnostics are uploaded automatically
- No account, email address, phone number, or device identifier is collected by
  the developers
- No advertising or cross-site tracking identifiers

The developers receive no information about you unless you submit feedback,
open an issue, or publish something referencing the project.

## Android application

The Android application stores its offline library in the app's private
storage. It connects to the same services listed above, and additionally to the
desktop instance on your local network when you use device synchronisation. In
that mode, data is transferred directly between your own devices over your
local network and is not routed through any third-party server.

## Retention and deletion

Local data is retained until you delete it. You can remove the data directory,
clear the offline library in the app, or uninstall the application. Public
feedback issues remain visible until they are deleted at your request.

## Children

Paperead is a research tool intended for adults and is not directed at
children.

## Changes

Material changes to this policy will be published in this file with an updated
date. The version history is available in the repository.

## Contact

Questions or deletion requests: open an issue at
`https://github.com/xiaohui-plus/Paperead/issues`.

---

## 中文摘要

Paperead 不收集任何遥测、使用统计或崩溃数据，也不需要注册账号。文献、笔记、翻译与设置都只保存在本机数据目录（Windows 为 `%LOCALAPPDATA%\Paperead\data`）。

只有在使用对应功能时才会产生网络请求：调用你自己配置的模型接口（正文会发送到该接口）、检索文献（查询词发送到 arXiv / PubMed / OpenAlex / Europe PMC / Semantic Scholar / CORE）、Zotero 同步（到你自己的账号）、检查更新与下载运行时（gitee.com / github.com / ghproxy.net）、获取阅读笔记目录。

反馈功能是可选的，一旦提交，标题、描述和截图会写入公开仓库 `gitee.com/deng_xiaohui/paperead_issue`，任何人可见，请不要在反馈中填写隐私信息。模型 API Key 仅保存在本机，未加密存储，请自行保管设备。
