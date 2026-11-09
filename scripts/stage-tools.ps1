# 准备研究/综述写作需要的外部工具（目前只有 pandoc），供两处使用：
# - 便携版：build-portable.ps1 会把 .tmp\tools\pandoc.exe 放进 resources\tools，
#   便携包因此完全离线；
# - 安装包：不再内置 pandoc（116MB 会顶破 Gitee 100MB 附件上限），
#   改为首次启动自动下载，所以安装包只带走这里产出的 release\pandoc-*.zip 作为下载源。
# 3.x 的 pandoc.exe 有 220MB+；2.19 已经支持导出用到的 --natbib / task_lists / pipe_tables。
param(
    [string]$PandocVersion = "2.19.2"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cache = Join-Path $root ".tmp\tools"
$release = Join-Path $root "release"
New-Item -ItemType Directory -Force $cache | Out-Null
New-Item -ItemType Directory -Force $release | Out-Null

$asset = "pandoc-$PandocVersion-windows-x86_64.zip"
$zip = Join-Path $cache $asset
$target = Join-Path $cache "pandoc.exe"

if (-not (Test-Path $zip)) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $url = "https://github.com/jgm/pandoc/releases/download/$PandocVersion/$asset"
    Write-Host "DOWNLOAD $url"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
}

if (-not (Test-Path $target)) {
    $extract = Join-Path $cache "pandoc-$PandocVersion"
    Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    $exe = Get-ChildItem $extract -Recurse -Filter "pandoc.exe" | Select-Object -First 1
    if (-not $exe) { throw "压缩包里没有 pandoc.exe" }
    Copy-Item $exe.FullName $target -Force
}

# 安装包首次启动时按这个文件名下载，文件名和 sha256 都写死在 server/research/pandoc_setup.py。
$publish = Join-Path $release $asset
if (-not (Test-Path $publish)) { Copy-Item $zip $publish -Force }

$version = & $target --version | Select-Object -First 1
$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "STAGED pandoc: $target ($version, 下载源 $publish, $mb MB)"
exit 0
