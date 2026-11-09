# Paperead 打包发布通关手册

> 这份手册只解决一件事：**把能用的东西装进安装包，并且让它出现在下载页上。**
> 每次发版前照着走一遍，能避开我们踩过的所有坑。

---

## 一、先记住一句话

**构建顺序大于一切：sidecar → agent → tools → 前端 → 安装包。**
任何一个环节漏了，装出来的软件都会「看起来正常，一用就废」。

```
set_version.py      改版本号（4 个文件一起改）
      │
build-sidecar.ps1   把 server/ + paperead/ 冻成 86MB 的 paperead-server.exe
      │
stage-agent.ps1     把 agent/ 拷进 resources，顺手删掉 136MB 没用的东西
      │
stage-tools.ps1     准备 pandoc：便携版带上，安装包只带走下载源
      │
tauri build         前端 + Rust 壳 + 资源，压成 NSIS 安装包
      │
verify              装一遍、点一遍、跑一次导出（首次会下载 pandoc）
      │
publish             GitHub 自动 / Gitee 手动
```

---

## 二、三条咒语（直接抄）

```powershell
# 1) 一条龙构建（sidecar / agent / pandoc 下载源，签名自动用 prod 密钥）
npm run build:installer

# 2) 便携版 + 发布物料
npm run build:portable

# 3) 发版前体检（版本号、签名、SHA256、pytest）
npm run verify:release
```

产物在这里：

| 东西 | 位置 | 体积参考 |
| --- | --- | --- |
| 安装包 | `src-tauri/target/release/bundle/nsis/Paperead_x.y.z_x64-setup.exe` | 70 MB 左右（不含 pandoc） |
| 更新签名 | 同目录 `.exe.sig` | 1 KB |
| 便携版 | `release/Paperead_x.y.z_portable_x64.zip` | 190 MB 左右（自带 pandoc） |
| pandoc 下载源 | `release/pandoc-2.19.2-windows-x86_64.zip` | 24 MB |
| 校验和 | `release/SHA256SUMS.txt` | 1 KB |

---

## 三、最容易翻车的五个地方

### 坑 1：`tauri build` 不会重建 Python sidecar

**症状**：改了 `server/`，装完新包发现行为没变。
**原因**：Tauri 只打包 `src-tauri/resources/paperead-server` 里**已经存在**的那个 exe。
**解法**：先 `npm run build:sidecar`（或直接用 `build:installer`，它已经帮你串好了）。

> 本次就栽在这里：`find_pandoc` 改完、包也打了，结果安装包里还是旧 sidecar，导出依旧报「服务器未找到 pandoc」。

---

### 坑 2：F 盘不允许执行 exe

**症状**：装包静默安装退出码 2，什么都没装上；换最小 NSIS 探针也一样。
**原因**：不是脚本问题，是这台机器的 F 盘策略。
**解法**：把安装包复制到 `$env:TEMP` 再运行。

```powershell
Copy-Item "src-tauri\target\release\bundle\nsis\Paperead_0.1.5_x64-setup.exe" "$env:TEMP\p.exe" -Force
Start-Process "$env:TEMP\p.exe" -ArgumentList "/S" -Wait
```

---

### 坑 3：`\\?\C:\...` 让研究 Agent 原地去世

**症状**：安装版一打开就退出；`agent.log` 里写着

```
Error: EISDIR: illegal operation on a directory, lstat 'C:'
    at resolveMainPath
```

**原因**：Tauri 的 `resource_dir()` 在 Windows 返回长路径前缀 `\\?\`，Node 解析 `bin.mjs` 时把 `C:` 当成目录。
**解法**：在 [agent_sidecar.rs](../src-tauri/src/agent_sidecar.rs) 里用 `plain_path()` 去掉前缀（`\\?\UNC\` 要还原成 `\\`）。

**自查口诀**：装完先看 `%LOCALAPPDATA%\paperead-data\logs\agent.log` 有没有 `ready`。

---

### 坑 4：Gitee 单个附件上限 100 MB

**症状**：上传返回 413 / 提示附件过大。
**原因**：Gitee 发行版附件单文件 100 MB 封顶。
**0.1.6 的解法**：**安装包不再内置 pandoc**（它一个人就 117 MB），改成首次启动自动下载：

- `stage-tools.ps1` 只把 pandoc 缓存在 `.tmp/tools`，并复制一份 `release/pandoc-2.19.2-windows-x86_64.zip` 作为下载源
- 安装包只带 sidecar + agent，体积回落到 70 MB 左右
- 便携版仍由 `build-portable.ps1` 把 pandoc 放进 `resources/tools`，完全离线
- 下载地址和 sha256 在 `server/research/pandoc_setup.py`；Gitee 发行版必须把 `pandoc-*.zip` 一起传上去

**其他省体积手段**（仍然有效）：

- `stage-agent.ps1` 删掉 pdb / sourcemap / `.d.ts` / 测试 / 文档 / 非 x64 预编译：agent 333 MB → 197 MB
- 便携版仍然 > 100 MB，只发 GitHub，Gitee 不放

---

### 坑 5：tag 推早了

**症状**：CI 用旧代码构建，后面的修复没进这次 release。
**原因**：`release.yml` 由 `push v*` 触发，tag 指向哪个 commit 就构建哪个 commit，**之后再改代码不会重跑**。
**解法**：

- 先把代码、脚本、版本号全部改完，**最后才打 tag**
- 已经发出去就别 force 改 tag，直接发下一个 patch 版本（0.1.5 → 0.1.6）
- 本地构建和 CI 构建可能体积不同，功能一致即可，不必强行对齐

---

## 四、验证：不看日志不算数

装完之后至少拿到这三条证据：

| 证据 | 怎么看 |
| --- | --- |
| 应用不闪退 | 进程还在，`app.log` 有 `desktop ready` |
| 研究 Agent 活着 | `agent.log` 有 `[paperead-agent] ready pid=...` |
| 导出真的能用 | 删掉数据目录的 `tools\pandoc` 再启动，等自动下载完成后，在没有系统 pandoc 的 PATH 下导出 DOCX 和 LaTeX 各生成一个文件 |

最后一条可以照搬这次的临时脚本思路（`.tmp/verify-research.ps1`）：

1. 先确认 `%LOCALAPPDATA%\paperead-data\tools\pandoc\pandoc.exe` 已由应用自动下载（或调 `POST /api/toolchain/pandoc` 触发）
2. 用 `PAPEREAD_PORT/PAPEREAD_DATA/PAPEREAD_TOKEN/PAPEREAD_INTERNAL_TOKEN` 启动已安装的 sidecar
3. 把 `PATH` 砍到只剩 `C:\Windows\System32`，确保找不到本机 pandoc
4. `POST /api/research/projects` → `POST .../sessions` → `POST /internal/research/sessions/{id}/export`
5. 检查 `files=1`、`error=` 为空、磁盘上真有 `.docx`

LaTeX 相关单元测试：

```powershell
.venv-build\Scripts\python.exe -m pytest tests\test_research_latex.py -q
```

---

## 五、发布：GitHub 自动，Gitee 手动

### GitHub

1. 推 tag：`git push origin v0.1.5`
2. Actions 跑完会生成 **draft** release
3. 手动把它变成正式版：

```powershell
# token 从 git 凭据里取，别打印出来
$cred = "protocol=https`nhost=github.com`n`n" | git credential fill
$tok  = ($cred | Select-String 'password=(.+)').Matches.Groups[1].Value
$h    = @{ Authorization = "Bearer $tok"; Accept = "application/vnd.github+json" }

$body = '{"draft":false,"prerelease":false,"make_latest":"true"}'
Invoke-RestMethod -Uri "https://api.github.com/repos/xiaohui-plus/Paperead/releases/<id>" `
  -Method Patch -Headers $h -ContentType "application/json" -Body $body
```

### Gitee

1. 先推 tag 和分支：`git push gitee v0.1.5`、`git push gitee feat/desktop-m0:master`
2. 建发行版：`POST https://gitee.com/api/v5/repos/deng_xiaohui/paperead/releases`
3. 传附件：`POST .../releases/{id}/attach_files?access_token=...`，multipart 字段名固定是 `file`

Gitee 只传这六个：安装包、安装包 zip、`pandoc-2.19.2-windows-x86_64.zip`、源码 zip、`latest.json`、`SHA256SUMS.txt`。
便携版走 GitHub。pandoc 那个 zip 是安装包首次启动的下载源，漏传的话用户导出会一直失败。

### 别忘了

- README 顶部三条下载链接同步到新版本号
- `make_latest_json.py` 里的 `--url` 指向新 tag
- 图标改动用 `npx tauri icon src-tauri/icons/app-icon.svg`，资源管理器还显示旧图就 `ie4uinit.exe -show`

---

## 六、发版检查清单（打印出来贴墙上）

- [ ] 版本号 `0.1.x` 已用 `scripts/set_version.py` 改完
- [ ] `npm run build:installer` 成功，安装包 < 100 MB
- [ ] `npm run build:portable` 成功（自带 pandoc）
- [ ] `npm run verify:release` 通过
- [ ] 装一遍：进程在、agent ready、pandoc 自动下载成功、docx/latex 导出成功
- [ ] `release/pandoc-2.19.2-windows-x86_64.zip` 已随发行版上传（GitHub + Gitee）
- [ ] 桌面快捷方式图标是蓝底白 P
- [ ] 代码已推到 GitHub `main` 和 Gitee `master`
- [ ] tag 指向**最终** commit
- [ ] GitHub draft 已转为正式 release 并设为 latest
- [ ] Gitee 发行版附件齐全、下载链接返回 200
- [ ] README 下载链接已更新

---

## 七、速查表

| 现象 | 第一反应 |
| --- | --- |
| 装了新包但功能还是旧的 | sidecar 没重建 |
| 安装包退出码 2 | 换到 `%TEMP%` 再装 |
| 应用闪退，日志 `lstat 'C:'` | `\\?\` 前缀，看 `agent_sidecar.rs` |
| 上传 Gitee 失败 | 单文件超 100 MB |
| 导出提示找不到 pandoc | 等自动下载完成，或看发行版有没有 `pandoc-*.zip` |
| CI 构建的内容不对 | tag 打早了，发新版本 |
| 签名报密码错误 | 用了 dev 密钥配 prod 密码 |
| GitHub API 403 | 没带 token 或触发限流，用 `git credential fill` 取 |
| 图标还是旧文档图形 | 包没换图标，或 Windows 图标缓存 |
