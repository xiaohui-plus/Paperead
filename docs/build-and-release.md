# Paperead 打包发布通关手册

> 这份手册只解决一件事：**把能用的东西装进安装包，并且让它出现在下载页上。**
> 每次发版前照着走一遍，能避开我们踩过的所有坑。

> ⚠️ **核心原则：所有版本必须同时发布到 GitHub 和 Gitee。**
> 两个平台的版本号、更新说明、安装包要完全一致，不能先发一个再补另一个。
> README 顶部的下载链接用 `releases/latest` 自动指向最新版，不用每次手动改。

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

# 4) 安卓 APK（arm64，release 签名）
npm run build:android
```

产物在这里：

| 东西 | 位置 | 体积参考 |
| --- | --- | --- |
| 安装包 | `src-tauri/target/release/bundle/nsis/Paperead_x.y.z_x64-setup.exe` | 70 MB 左右（不含 pandoc） |
| 更新签名 | 同目录 `.exe.sig` | 1 KB |
| 便携版 | `release/Paperead_x.y.z_portable_x64.zip` | 190 MB 左右（自带 pandoc） |
| 安卓 APK | `release/Paperead_x.y.z_android-arm64.apk` | 11 MB 左右 |
| pandoc 下载源 | `release/pandoc-2.19.2-windows-x86_64.zip` | 24 MB |
| 校验和 | `release/SHA256SUMS.txt` | 1 KB |

> 安装包还要复制成 `release/Paperead_x.y.z_x64-setup.exe`（连 `.sig` 一起），`release-publish.ps1` 从这里取物料。

---

## 三、最容易翻车的六个地方

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

### 坑 6：安卓构建链是本地私有的

**症状**：`npm run build:android` 报找不到 JDK / SDK / NDK，或 Kotlin 编译报中文路径乱码。
**原因**：安卓工具链不在 CI 里，全靠本机；`JAVA_HOME` 这类系统变量还可能是失效路径（例如指向已删除的 `D:\java`）。
**解法**：`scripts/build-android.ps1` 会先校验目录是否存在，失效就回落到 `F:\Agent\toolchains\` 下的固定工具链：

| 东西 | 路径 |
| --- | --- |
| JDK | `F:\Agent\toolchains\jdk\jdk-17.0.21+9` |
| Android SDK | `F:\Agent\toolchains\android-sdk`（platform 35/36/37、build-tools 35/36/37） |
| NDK | `F:\Agent\toolchains\android-sdk\ndk\27.2.12479018` |
| CARGO_HOME | `F:\Agent\toolchains\cargo-home`（纯 ASCII，避开中文用户名） |

- Rust 需要 `aarch64-linux-android` 等 target：`rustup target add aarch64-linux-android`
- 签名密钥在 `src-tauri/gen/android/paperead-release.keystore` 与 `keystore.properties`（都被 gitignore），**务必离线备份**，换密钥会导致老用户无法覆盖升级
- APK 只出 arm64（`Paperead_x.y.z_android-arm64.apk`，约 11 MB），模拟器/其他架构要自己加 `-Target`

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

## 五、发布：双平台同步，缺一不可

**原则：GitHub 和 Gitee 必须在同一次发布动作里都更新，版本号、更新说明、附件完全一致。**
只发一个平台等于漏发一半用户。

### 一条命令收尾（推荐）

构建、验证完成后，直接跑发布脚本：

```powershell
$env:GITEE_TOKEN = "私人令牌（需要 releases 权限）"
npm run release:publish -- -Version 0.1.7
```

脚本（`scripts/release-publish.ps1`）依次做四件事：

1. **GitHub**：把 CI 生成的 draft release 转正并设为 latest；Gitee 描述自动取 GitHub 更新说明，两边一字不差
2. **Gitee 版本发行版**：不存在就创建（tag 必须先推上去），再上传安装包、安卓 APK、`pandoc-*.zip`、`latest.json`、`SHA256SUMS.txt`，同名附件自动替换；可选 `-SourceZip` 补传源码 zip
3. **Gitee `latest` 固定发行版**：不存在就创建，并把 `latest.json` 附件替换成本次版本
4. **下载自检**：Gitee 安装包和 latest.json 直链必须返回 200，且 latest.json 内嵌版本一致，否则退出码 1

常用参数：`-SkipGithub` / `-SkipGitee` 只发一边（收尾补漏时用）、`-Notes "说明"` 覆盖更新描述。

下面的手动流程保留作参考（脚本报错时按此排查）：

### GitHub（自动构建 + 手动转正）

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

GitHub 附件：安装包、便携版、**安卓 APK**、`pandoc-*.zip`、源码 zip、`latest.json`、`SHA256SUMS.txt`。
其中安卓 APK 是本地构建的，CI 手里没有：`release-publish.ps1` 在把 draft 转正之前会先补传，所以 **先跑 `npm run build:android` 再发版**。

### Gitee（手动）

1. 先推 tag 和分支：`git push gitee v0.1.5`、`git push gitee feat/desktop-m0:master`
2. 建发行版：`POST https://gitee.com/api/v5/repos/deng_xiaohui/paperead/releases`
3. 传附件：`POST .../releases/{id}/attach_files?access_token=...`，multipart 字段名固定是 `file`

Gitee 只传这六个：安装包、**安卓 APK**、安装包 zip、`pandoc-2.19.2-windows-x86_64.zip`、源码 zip、`latest.json`、`SHA256SUMS.txt`。
便携版走 GitHub（> 100 MB 超 Gitee 限制）。pandoc 那个 zip 是安装包首次启动的下载源，漏传的话用户导出会一直失败。

**另外必须维护一个固定 tag 为 `latest` 的发行版**（脚本会自动创建和替换）：
Gitee 没有 `releases/latest/download/` 永久直链，应用的更新检查端点第一位就是
`https://gitee.com/deng_xiaohui/paperead/releases/download/latest/latest.json`。
每次发版后把它的 `latest.json` 附件替换成本次版本，漏更新的话已装用户永远检查不到新版本：

```powershell
# 1) 按 tag 找到 latest 发行版，记下 release id 和 latest.json 附件的 attach id
Invoke-RestMethod "https://gitee.com/api/v5/repos/deng_xiaohui/paperead/releases/tags/latest"

# 2) 删掉旧的 latest.json 附件
Invoke-RestMethod -Method Delete `
  "https://gitee.com/api/v5/repos/deng_xiaohui/paperead/releases/{release_id}/attach_files/{attach_id}?access_token=$tok"

# 3) 重新上传（同建发行版的 attach_files 接口，multipart 字段名固定是 file）
```

> 注意：这份 `latest.json` 里内嵌的安装包 `url` 要指向 **Gitee 下载直链**，这样国内用户更新时的下载流量也走 Gitee。

> ⚠️ **不要在 Gitee 描述里写死 GitHub 的直链**。Gitee 的安装包应上传到 Gitee 自身，避免国内用户访问 GitHub 附件失败。

### 更新链路：用户从哪里拉

已装用户的「更新」不是重新下载安装包，而是走自动更新链路。每一段从哪个平台拉，发布前要心里有数：

| 环节 | 从哪拉 | 说明 |
| --- | --- | --- |
| 检查新版本 | `latest.json` 清单 | **Gitee `latest` 固定发行版优先**，GitHub Releases（ghproxy 镜像兜底）紧随其后；可用 `PAPEREAD_UPDATE_ENDPOINT` 环境变量覆盖 |
| 下载新安装包 | `latest.json` 里内嵌的 `url` | CI（tauri-action）自动生成时指向 GitHub；手动重签名生成时用 `make_latest_json.py --url`，国内用户建议指向 Gitee 安装包直链 |
| pandoc 首次启动下载 | **Gitee 优先**，ghproxy / GitHub 兜底 | 顺序写在 `pandoc_setup.py` 里；Gitee 漏传 `pandoc-*.zip`，国内用户导出会一直失败 |

> 结论：**Gitee 是国内用户的拉取主通道**。发布时 Gitee 附件必须齐全、及时，否则国内用户「装得上、更不动、导不出」。

### 发布后核对

- [ ] GitHub Release 已可见，附件可下载
- [ ] Gitee Release 已可见，附件可下载
- [ ] 两边版本号、更新说明一字不差
- [ ] 安装包与便携版是同一次构建的产物
- [ ] `latest.json` 可访问、内嵌下载 url 有效（已装用户的自动更新依赖它）

### 别忘了

- README 顶部两条下载链接（GitHub + Gitee）已同步
- `make_latest_json.py` 里的 `--url` 指向 **Gitee 安装包直链**（国内拉取主通道），例如 `https://gitee.com/deng_xiaohui/paperead/releases/download/v0.1.7/Paperead_0.1.7_x64-setup.exe`
- 图标改动用 `npx tauri icon src-tauri/icons/app-icon.svg`，资源管理器还显示旧图就 `ie4uinit.exe -show`

---

## 六、发版检查清单（打印出来贴墙上）

- [ ] 版本号 `0.1.x` 已用 `scripts/set_version.py` 改完
- [ ] `npm run build:installer` 成功，安装包 < 100 MB
- [ ] `npm run build:portable` 成功（自带 pandoc）
- [ ] `npm run build:android` 成功，`release/Paperead_x.y.z_android-arm64.apk` 已生成
- [ ] `npm run verify:release` 通过
- [ ] 装一遍：进程在、agent ready、pandoc 自动下载成功、docx/latex 导出成功
- [ ] `release/pandoc-2.19.2-windows-x86_64.zip` 已随发行版上传（GitHub + Gitee）
- [ ] 桌面快捷方式图标是蓝底白 P
- [ ] 代码已推到 GitHub `main` 和 Gitee `master`
- [ ] tag 指向**最终** commit
- [ ] GitHub draft 已转为正式 release 并设为 latest
- [ ] Gitee 发行版已创建，附件齐全、下载链接返回 200
- [ ] Gitee `latest` 固定发行版的 `latest.json` 已替换为本次版本（更新检查第一位端点）
- [ ] **两个平台版本号、更新说明、安装包完全一致**
- [ ] `latest.json` 可访问、内嵌下载 url 有效（国内拉取主通道）
- [ ] README 下载链接（GitHub + Gitee）已同步

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
