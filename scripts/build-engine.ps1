# 构建可下载的 BabelDOC 引擎分卷。
#
# 安装包不内置引擎：首次启动由 server/babeldoc_setup.py 从发行版下载这些分卷。
# 产物：
#   release\babeldoc-engine-<ver>-windows-x86_64.zip
#   release\babeldoc-engine-<ver>-windows-x86_64.zip.partNNN   （每个 <= PartMB）
#   release\babeldoc-engine-<ver>.json                          （分卷清单 + SHA-256）
#
# 用法：
#   powershell -File scripts\build-engine.ps1
#   powershell -File scripts\build-engine.ps1 -Version 0.7.1 -Source <已装好的引擎目录>
#
# 不传 -Source 时用 uv 现场装一份便携引擎（联网，约 600MB）。

param(
    [string]$Version = "0.7.1",
    [string]$Source = "",
    [string]$OutDir = "",
    [int]$PartMB = 90
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$platform = "windows-x86_64"
if (-not $OutDir) { $OutDir = Join-Path $root "release" }
New-Item -ItemType Directory -Force $OutDir | Out-Null

function Write-Runner([string]$dir) {
    $text = @'
"""BabelDOC 便携入口：用随包 Python 直接调用 CLI，避免 Scripts\babeldoc.exe 写死旧路径。"""
import sys

from babeldoc.main import cli

if __name__ == "__main__":
    sys.exit(cli())
'@
    [IO.File]::WriteAllText(
        (Join-Path $dir "run_babeldoc.py"),
        $text + "`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
}

if (-not $Source) {
    $cache = Join-Path $root ".tmp\engine-python"
    $build = Join-Path $root ".tmp\engine-build\$Version"
    New-Item -ItemType Directory -Force $cache | Out-Null

    Write-Host "准备便携 Python 3.13 ..."
    uv python install 3.13 --install-dir $cache | Out-Null
    $pyDir = Get-ChildItem $cache -Directory |
        Where-Object { $_.Name -match '^cpython-3\.13\.\d+-' -and (Test-Path (Join-Path $_.FullName "python.exe")) } |
        Select-Object -First 1
    if (-not $pyDir) { throw "uv 没有装出可用的 Python 3.13" }

    if (Test-Path $build) { Remove-Item -Recurse -Force $build }
    New-Item -ItemType Directory -Force $build | Out-Null
    Write-Host "复制便携 Python ..."
    robocopy $pyDir.FullName (Join-Path $build "python") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制 Python 失败（robocopy $LASTEXITCODE）" }
    Remove-Item (Join-Path $build "python\Lib\EXTERNALLY-MANAGED") -Force -ErrorAction SilentlyContinue

    Write-Host "安装 BabelDOC $Version ..."
    uv pip install --python (Join-Path $build "python\python.exe") --break-system-packages "babeldoc==$Version"
    if ($LASTEXITCODE -ne 0) { throw "安装 BabelDOC 失败" }
    Write-Runner $build
    $Source = $build
}

if (-not (Test-Path (Join-Path $Source "python\python.exe"))) {
    throw "引擎目录缺少 python\python.exe：$Source"
}
if (-not (Test-Path (Join-Path $Source "run_babeldoc.py"))) {
    Write-Runner $Source
}

# 只删运行时用不到的开发/构建产物；site-packages 的 *.dist-info 必须保留，
# 有些包会在运行期用 importlib.metadata 查版本。
Write-Host "裁剪无关文件 ..."
$pruneDirs = @(
    "python\Lib\test",
    "python\Lib\idlelib",
    "python\Lib\tkinter",
    "python\Lib\turtledemo",
    "python\Lib\ensurepip",
    "python\Lib\lib2to3",
    "python\Lib\pydoc_data",
    "python\include",
    "python\libs",
    "python\tcl",
    "python\Scripts",
    "python\Lib\site-packages\pip",
    "python\Lib\site-packages\setuptools",
    "python\Lib\site-packages\wheel"
)
foreach ($rel in $pruneDirs) {
    $path = Join-Path $Source $rel
    if (Test-Path $path) { Remove-Item -Recurse -Force $path -ErrorAction SilentlyContinue }
}
Get-ChildItem $Source -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $Source -Recurse -File -Include *.pyc, *.pyo -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
foreach ($name in @("_tkinter.pyd", "tcl86t.dll", "tk86t.dll")) {
    Get-ChildItem (Join-Path $Source "python\DLLs") -Filter $name -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

$mb = [math]::Round(((Get-ChildItem $Source -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "引擎目录：$Source（$mb MB）"

$zip = Join-Path $OutDir "babeldoc-engine-$Version-$platform.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Write-Host "压缩 ..."
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory(
    $Source, $zip, [IO.Compression.CompressionLevel]::Optimal, $false
)
$size = (Get-Item $zip).Length
$sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
Write-Host ("ZIP：{0}（{1} MB）" -f (Split-Path -Leaf $zip), [math]::Round($size / 1MB, 1))

# 按固定大小切分，单卷必须低于 Gitee 100MB 附件限制（默认 90MB）。
Write-Host "切分分卷 ..."
Get-ChildItem $OutDir -Filter "babeldoc-engine-$Version-$platform.zip.part*" -ErrorAction SilentlyContinue |
    Remove-Item -Force
$partSize = [int64]$PartMB * 1MB
$parts = @()
$input = [IO.File]::OpenRead($zip)
try {
    $buffer = New-Object byte[] (4MB)
    $index = 0
    while ($true) {
        $index++
        $name = "babeldoc-engine-$Version-$platform.zip.part{0:D3}" -f $index
        $path = Join-Path $OutDir $name
        $part = [IO.File]::Create($path)
        $written = [int64]0
        try {
            while ($written -lt $partSize) {
                $want = [Math]::Min([int64]$buffer.Length, $partSize - $written)
                $read = $input.Read($buffer, 0, [int]$want)
                if ($read -le 0) { break }
                $part.Write($buffer, 0, $read)
                $written += $read
            }
        } finally {
            $part.Close()
        }
        if ($written -eq 0) {
            Remove-Item $path -Force
            break
        }
        $parts += $name
        if ($written -lt $partSize) { break }
    }
} finally {
    $input.Close()
}

$manifestPath = Join-Path $OutDir "babeldoc-engine-$Version.json"
$manifest = [ordered]@{
    version  = $Version
    platform = $platform
    asset    = (Split-Path -Leaf $zip)
    parts    = $parts
    size     = [int64]$size
    sha256   = $sha
}
[IO.File]::WriteAllText(
    $manifestPath,
    ($manifest | ConvertTo-Json -Depth 4),
    (New-Object System.Text.UTF8Encoding($false))
)

Write-Host "MANIFEST: $manifestPath"
foreach ($p in $parts) {
    $path = Join-Path $OutDir $p
    Write-Host ("  {0}  {1} MB" -f $p, [math]::Round((Get-Item $path).Length / 1MB, 1))
}
Write-Host "ENGINE BUILT: $Version（$($parts.Count) 个分卷）"
