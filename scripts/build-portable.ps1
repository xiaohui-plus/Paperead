$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$exe = Join-Path $root "src-tauri\target\release\paperead-desktop.exe"
$res = Join-Path $root "src-tauri\resources"
if (-not (Test-Path $exe)) { throw "缺少 release exe，先运行 npm run build:installer" }
if (-not (Test-Path (Join-Path $res "paperead-server\paperead-server.exe"))) { throw "缺少 sidecar resources" }
if (-not (Test-Path (Join-Path $res "agent\src\bin.mjs"))) { throw "缺少 agent resources，先运行 scripts\stage-agent.ps1" }

$config = Get-Content (Join-Path $root "src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json
$version = $config.version
$stage = Join-Path $root "tmp-portable-stage"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null

Copy-Item $exe (Join-Path $stage "paperead-desktop.exe") -Force
New-Item -ItemType Directory -Force (Join-Path $stage "resources") | Out-Null
Copy-Item -Recurse -Force (Join-Path $res "*") (Join-Path $stage "resources")

# 便携版自带 pandoc，解压就能离线导出；安装包为了控制体积走首次启动自动下载。
$pandoc = Join-Path $root ".tmp\tools\pandoc.exe"
if (Test-Path $pandoc) {
    $pandocDir = Join-Path $stage "resources\tools\pandoc"
    New-Item -ItemType Directory -Force $pandocDir | Out-Null
    Copy-Item $pandoc (Join-Path $pandocDir "pandoc.exe") -Force
} else {
    Write-Warning "没有 .tmp\tools\pandoc.exe，便携版首次导出时会自动下载 pandoc"
}

$launcher = @"
@echo off
setlocal
set "PAPEREAD_DATA=%~dp0data"
start "" "%~dp0paperead-desktop.exe"
"@
Set-Content (Join-Path $stage "启动.cmd") $launcher -Encoding ASCII

$readme = @"
Paperead 便携版（Windows 10/11 x64）
=========================================

1. 解压整个文件夹到任意可写目录（不要只解压 exe）。
2. 双击「启动.cmd」。
3. 首次启动会提示填写模型 API Key。
4. PDF、阅读数据、日志都在本目录的 data\ 下，删除 data\ 即可重置。
5. 更新：下载新版便携 ZIP，覆盖 exe 和 resources\，保留 data\。

不需要安装 Python 或 Node。需要系统有 WebView2。
"@
Set-Content (Join-Path $stage "README.txt") $readme -Encoding UTF8

$zip = Join-Path $root ("release\Paperead_" + $version + "_portable_x64.zip")
New-Item -ItemType Directory -Force (Split-Path $zip) | Out-Null
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal -Force
$size = [math]::Round((Get-Item $zip).Length / 1MB, 2)
Write-Host "PORTABLE: $zip ($size MB)"
