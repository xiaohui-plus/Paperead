$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$exe = Join-Path $root "src-tauri\target\release\paper-reflow-desktop.exe"
$res = Join-Path $root "src-tauri\resources\paper-reflow-server"
if (-not (Test-Path $exe)) { throw "缺少 release exe，先运行 npm run build:installer" }
if (-not (Test-Path (Join-Path $res "paper-reflow-server.exe"))) { throw "缺少 sidecar resources" }

$config = Get-Content (Join-Path $root "src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json
$version = $config.version
$stage = Join-Path $root "tmp-portable-stage"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null

Copy-Item $exe (Join-Path $stage "paper-reflow-desktop.exe") -Force
New-Item -ItemType Directory -Force (Join-Path $stage "resources") | Out-Null
Copy-Item -Recurse -Force $res (Join-Path $stage "resources\paper-reflow-server")

$launcher = @"
@echo off
setlocal
set "PAPER_REFLOW_DATA=%~dp0data"
start "" "%~dp0paper-reflow-desktop.exe"
"@
Set-Content (Join-Path $stage "启动.cmd") $launcher -Encoding ASCII

$readme = @"
paper-reflow 便携版（Windows 10/11 x64）
=========================================

1. 解压整个文件夹到任意可写目录（不要只解压 exe）。
2. 双击「启动.cmd」。
3. 首次启动会提示填写模型 API Key。
4. PDF、阅读数据、日志都在本目录的 data\ 下，删除 data\ 即可重置。
5. 更新：下载新版便携 ZIP，覆盖 exe 和 resources\，保留 data\。

不需要安装 Python 或 Node。需要系统有 WebView2。
"@
Set-Content (Join-Path $stage "README.txt") $readme -Encoding UTF8

$zip = Join-Path $root ("release\paper-reflow_" + $version + "_portable_x64.zip")
New-Item -ItemType Directory -Force (Split-Path $zip) | Out-Null
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal -Force
$size = [math]::Round((Get-Item $zip).Length / 1MB, 2)
Write-Host "PORTABLE: $zip ($size MB)"
