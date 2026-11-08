# SignPath 开源免费签名接入

目标：让 Windows 安装包带上可信的 Authenticode 签名，减少 SmartScreen
“未知发布者”和杀软拦截。

## 不需要上传证书

SignPath Foundation 的代码签名证书和私钥保存在 SignPath 的 HSM 中。项目方
**不需要购买 PFX、不需要上传私钥**。CI 把未签名产物上传给 SignPath，SignPath
签好后返回已签名产物。

## 1. 申请

在 <https://signpath.org/apply> 提交申请，填写：

- 项目：paper-reflow
- 仓库：https://github.com/xiaohui-plus/Paperead
- 许可证：AGPL-3.0
- 构建方式：GitHub Actions（`.github/workflows/release.yml`）
- 代码签名策略：仓库根目录的 [CODE_SIGNING.md](../CODE_SIGNING.md)

审核通过后，你会得到：

- SignPath Organization ID（UUID）
- Project slug
- Signing policy slug
- Artifact configuration slug

## 2. 安装 GitHub App

在 SignPath 组织设置里安装 **SignPath GitHub App**，授权本仓库。SignPath
需要读取 Actions 运行记录来验证产物确实来自这个仓库。

## 3. 配置 Artifact Configuration

最小版本：只签 NSIS 安装包。上传的 artifact 是一个 ZIP，里面是 setup.exe：

```xml
<artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
  <zip-file>
    <pe-file path="paper-reflow_*_x64-setup.exe">
      <authenticode-sign/>
    </pe-file>
  </zip-file>
</artifact-configuration>
```

更完整的版本需要签 exe 内部的程序，但 NSIS 不支持像 MSI 那样的 deep-sign。
建议分两步：

1. 先用 `tauri build --no-bundle` 产出 `paper-reflow-desktop.exe`，把
   `paper-reflow-desktop.exe` 和 sidecar 的 `paper-reflow-server.exe` 打包成
   ZIP，交给 SignPath 签；
2. 用已签名的 exe + sidecar 重新执行 NSIS 打包；
3. 再把最终安装包交给 SignPath 签一次；
4. 最后用 Tauri 更新私钥对**已签名的安装包**重新生成 `.sig` 和 `latest.json`。

第 4 步很关键：Authenticode 签名会改变文件字节，Tauri 自动更新的 `.sig`
必须对最终安装包重新生成，否则更新校验失败。

## 4. GitHub 配置

在仓库 **Settings → Secrets and variables → Actions** 中添加：

Secrets：

- `SIGNPATH_API_TOKEN`：SignPath 用户设置里创建的 API token（submitter 权限）

Variables：

- `SIGNPATH_ORGANIZATION_ID`
- `SIGNPATH_PROJECT_SLUG`
- `SIGNPATH_SIGNING_POLICY_SLUG`
- `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG`

不需要上传证书文件。

## 5. Workflow 接入

核心步骤：

```yaml
- uses: actions/upload-artifact@v4
  id: unsigned
  with:
    name: unsigned-installer
    path: src-tauri/target/release/bundle/nsis/*.exe

- uses: signpath/github-action-submit-signing-request@v3
  id: signpath
  with:
    api-token: ${{ secrets.SIGNPATH_API_TOKEN }}
    organization-id: ${{ vars.SIGNPATH_ORGANIZATION_ID }}
    project-slug: ${{ vars.SIGNPATH_PROJECT_SLUG }}
    signing-policy-slug: ${{ vars.SIGNPATH_SIGNING_POLICY_SLUG }}
    artifact-configuration-slug: ${{ vars.SIGNPATH_ARTIFACT_CONFIGURATION_SLUG }}
    github-artifact-id: ${{ steps.unsigned.outputs.artifact-id }}
    wait-for-completion: true
    output-artifact-directory: signed

- name: Replace release installer with signed
  shell: pwsh
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  run: |
    $exe = Get-ChildItem signed -Recurse -Filter "*.exe" | Select-Object -First 1
    gh release upload "${{ github.ref_name }}" $exe.FullName --clobber
```

参考：

- <https://docs.signpath.io/trusted-build-systems/github>
- <https://docs.signpath.io/artifact-configuration/examples>

## 当前状态

- 尚未申请/接入 SignPath；Release 产物目前未做 Windows 代码签名。
- Tauri 自动更新签名已经完成，和 Authenticode 是两套独立的签名。
- 等 SignPath 申请通过、拿到 Organization/Project/Policy 配置后，再接入
  workflow；接入时必须重新生成 Tauri 更新签名。
