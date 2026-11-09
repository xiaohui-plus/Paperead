$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:PATH = "$env:USERPROFILE\.cargo\bin;$env:PATH"
$env:TEMP = Join-Path $root ".tmp"
$env:TMP = $env:TEMP
$env:npm_config_cache = Join-Path $root ".npm-cache"
New-Item -ItemType Directory -Force $env:TEMP | Out-Null

if (-not $env:TAURI_SIGNING_PRIVATE_KEY) {
    $prod = Join-Path $root ".tauri\updater.prod.key"
    $dev = Join-Path $root ".tauri\updater.key"
    if (Test-Path $prod) {
        $env:TAURI_SIGNING_PRIVATE_KEY = (Resolve-Path $prod).Path
        $passwordFile = Join-Path $root ".tauri\updater.prod.password.txt"
        if (-not $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD -and (Test-Path $passwordFile)) {
            $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = (Get-Content $passwordFile -Raw).Trim()
        }
    } elseif (Test-Path $dev) {
        $env:TAURI_SIGNING_PRIVATE_KEY = (Resolve-Path $dev).Path
    }
}

& (Join-Path $PSScriptRoot "build-sidecar.ps1")
if ($LASTEXITCODE -ne 0) { throw "sidecar 构建失败" }

& (Join-Path $PSScriptRoot "stage-agent.ps1")
if ($LASTEXITCODE -ne 0) { throw "agent 打包失败" }

# pandoc 不再进安装包（116MB 会顶破 Gitee 附件上限）：这里只准备下载源，
# 应用首次启动会自己去下载；便携版则由 build-portable.ps1 带上。
& (Join-Path $PSScriptRoot "stage-tools.ps1")
if ($LASTEXITCODE -ne 0) { throw "外部工具打包失败" }

npm run tauri -- build
if ($LASTEXITCODE -ne 0) { throw "Tauri 安装包构建失败" }

$bundle = Join-Path $root "src-tauri\target\release\bundle\nsis"
Get-ChildItem $bundle -Filter '*.exe' | Select-Object FullName, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 2) } }
