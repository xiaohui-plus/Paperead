# 产出 MathJax 用的 node 运行时精简包，供应用首次启动后台下载。
#
# 为什么只带 node.exe：完整 node 发行包约 30MB（解压后 90MB+，含 npm/corepack），
# 而公式渲染只需要一个可执行文件；打包后约 30MB，仍在 Gitee 单附件 100MB 以内。
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts/stage-node.ps1
# 产物：release\node-<version>-win-x64-lite.zip（同时打印 SHA256，需回填到
#       server/node_setup.py 的 ASSET_SHA256）
param(
    [string]$NodeVersion = "24.11.1"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 系统 %TEMP% 在部分机器上不可写，统一用项目内临时目录。
$env:TEMP = Join-Path $root ".tmp"
$env:TMP = $env:TEMP
$cache = Join-Path $env:TEMP "tools"
$release = Join-Path $root "release"
New-Item -ItemType Directory -Force $cache | Out-Null
New-Item -ItemType Directory -Force $release | Out-Null

$publish = Join-Path $release "node-$NodeVersion-win-x64-lite.zip"
if (Test-Path $publish) {
    Write-Host "REUSE $publish"
    $hash = (Get-FileHash -Algorithm SHA256 $publish).Hash
    Write-Host "SHA256 $hash"
    exit 0
}

$exe = Join-Path $cache "node-$NodeVersion.exe"
if (-not (Test-Path $exe)) {
    $url = "https://nodejs.org/dist/v$NodeVersion/win-x64/node.exe"
    Write-Host "DOWNLOAD $url"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
}

# 先确认拿到的是可运行的 node，避免把坏包发出去。
$version = & $exe --version 2>$null
if ($LASTEXITCODE -ne 0 -or -not $version) {
    throw "node.exe 无法运行（退出码 $LASTEXITCODE）"
}
if ("$version".Trim() -ne "v$NodeVersion") {
    throw "node 版本不匹配：期望 v$NodeVersion，实际 $version"
}

$stage = Join-Path $cache "node-lite-$NodeVersion"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item $exe (Join-Path $stage "node.exe") -Force

$zip = Join-Path $cache "node-$NodeVersion-win-x64-lite.zip"
Remove-Item -Force $zip -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $stage "node.exe") -DestinationPath $zip -CompressionLevel Optimal
Copy-Item $zip $publish -Force
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue

$size = (Get-Item $publish).Length
$hash = (Get-FileHash -Algorithm SHA256 $publish).Hash
Write-Host "STAGED $publish ($([math]::Round($size / 1MB, 1)) MB)"
Write-Host "SHA256 $hash"