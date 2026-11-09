# 把研究/综述写作需要的外部工具放进安装包资源目录。
# 目前只带 pandoc：DOCX 导出、Markdown→LaTeX 都靠它，用户机器不必再单独安装。
# 下载产物落在 .tmp\tools 做缓存，可重复执行。
# 3.x 的 pandoc.exe 有 220MB+，装进安装包会顶破 Gitee 100MB 附件上限；
# 2.19 已经支持导出用到的 --natbib / task_lists / pipe_tables，体积只有一半。
param(
    [string]$PandocVersion = "2.19.2"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root "src-tauri\resources\tools"
$pandocDir = Join-Path $tools "pandoc"
$cache = Join-Path $root ".tmp\tools"
New-Item -ItemType Directory -Force $pandocDir | Out-Null
New-Item -ItemType Directory -Force $cache | Out-Null

$target = Join-Path $pandocDir "pandoc.exe"
if (-not (Test-Path $target)) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $zip = Join-Path $cache "pandoc-$PandocVersion-windows-x86_64.zip"
    if (-not (Test-Path $zip)) {
        $url = "https://github.com/jgm/pandoc/releases/download/$PandocVersion/pandoc-$PandocVersion-windows-x86_64.zip"
        Write-Host "DOWNLOAD $url"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    }
    $extract = Join-Path $cache "pandoc-$PandocVersion"
    Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    $exe = Get-ChildItem $extract -Recurse -Filter "pandoc.exe" | Select-Object -First 1
    if (-not $exe) { throw "压缩包里没有 pandoc.exe" }
    Copy-Item $exe.FullName $target -Force
    Get-ChildItem $exe.Directory -Filter "*.dll" -ErrorAction SilentlyContinue |
        Copy-Item -Destination $pandocDir -Force
}

$version = & $target --version | Select-Object -First 1
$size = [math]::Round((Get-ChildItem $pandocDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "STAGED tools: $pandocDir ($size MB, $version)"
exit 0
