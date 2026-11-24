# 依赖发行版发布：BabelDOC 引擎 / node / pandoc 统一发到独立仓库。
#
# 为什么单独发：这三件与 App 版本解耦，挂在主仓库固定 latest 上时，每次 App
# 发版覆盖 latest 都可能把分卷覆盖丢（曾只剩 part003），客户端因此永远下载
# 失败而发布脚本仍显示成功。独立仓库的 tag 只增不改，客户端按清单 sha256 校验。
#
# 用法：
#   $env:GITEE_TOKEN = "私人令牌（需要 releases 权限）"
#   powershell -ExecutionPolicy Bypass -File scripts/publish-deps.ps1 `
#       -GiteeRepo deng_xiaohui/paperead-deps -Tag deps-latest
#
# 发布后须把客户端依赖源指向同一个仓库（server/deps_source.py 的默认值，
# 或部署时设置 PAPEREAD_DEPS_REPO），否则新版本仍走旧地址。

param(
    [string]$GiteeRepo = "deng_xiaohui/paperead-dependency",
    [string]$GiteeToken = $env:GITEE_TOKEN,
    # 依赖专用固定 tag：不要用 latest，避免与客户端更新检查的发行版混淆。
    [string]$Tag = "deps-latest",
    # 依赖仓库的分支名；Gitee 建发行版必须给 target_commitish。
    [string]$Commitish = "master",
    [string]$Dir = "",
    # 发布后完整下载每个文件并比对本地哈希（218MB 引擎会慢，默认只查可达与大小）。
    [switch]$VerifyHash
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not $Dir) { $Dir = Join-Path $root "release" }
if (-not (Test-Path $Dir)) { throw "找不到物料目录：$Dir" }
if (-not $GiteeToken) { throw "缺 GITEE_TOKEN（Gitee 私人令牌，需要 releases 权限）" }

# ---------- 收集依赖物料 ----------
$files = @(
    Get-ChildItem $Dir -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^babeldoc-engine-.*\.json$' -or
            $_.Name -match '^babeldoc-engine-.*\.zip\.part\d+$' -or
            $_.Name -match '^node-.*-lite\.zip$' -or
            $_.Name -match '^pandoc-.*-windows-x86_64\.zip$'
        }
)
if ($files.Count -eq 0) {
    throw "目录里没有任何依赖物料（引擎清单/分卷、node lite、pandoc）。先跑构建/打包脚本。"
}

$totalMb = [math]::Round((($files | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "待发布 $($files.Count) 个依赖文件，合计 $totalMb MB → $GiteeRepo@$Tag"

# ---------- Gitee API ----------
$giteeApi = "https://gitee.com/api/v5/repos/$GiteeRepo"

function Get-GiteeRelease([string]$TagName) {
    try {
        $res = Invoke-RestMethod -Uri "$giteeApi/releases/tags/$TagName" -TimeoutSec 60
        # Gitee 有时把单个发行版也包成数组；不摊平会让后面 $Release.id 变成数组
        if ($res -is [System.Array]) { $res = @($res) | Select-Object -First 1 }
        if (-not $res -or -not $res.id) { return $null }
        return $res
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch {}
        if ($code -eq 404) { return $null }
        throw "查询 Gitee 发行版 $TagName 失败（依赖仓库 $GiteeRepo 建好了吗？）：$($_.Exception.Message)"
    }
}

function New-GiteeRelease([string]$TagName, [string]$Name, [string]$Body, [string]$TargetCommitish) {
    # ContentType 必须带 charset=utf-8：PS 5.1 不带 charset 时按 ASCII 发，中文全变 "?"
    Invoke-RestMethod -Method Post `
        -Uri "$giteeApi/releases?access_token=$GiteeToken" `
        -ContentType "application/json; charset=utf-8" `
        -Body (@{ tag_name = $TagName; name = $Name; body = $Body; target_commitish = $TargetCommitish } | ConvertTo-Json) `
        -TimeoutSec 120
}

function Get-GiteeAttachments([int]$ReleaseId) {
    try {
        return @(Invoke-RestMethod -Uri "$giteeApi/releases/$ReleaseId/attach_files?access_token=$GiteeToken" -TimeoutSec 60)
    } catch {
        return @()
    }
}

function Add-GiteeFile([int]$ReleaseId, [string]$Path) {
    # PS 5.1 没有 -Form，手工拼 multipart；字段名固定是 file
    $fileName = Split-Path -Leaf $Path
    $bytes = [IO.File]::ReadAllBytes($Path)
    $boundary = "----paperead" + [guid]::NewGuid().ToString("N")
    $crlf = "`r`n"
    $head = [Text.Encoding]::UTF8.GetBytes(
        "--$boundary$crlf" +
        "Content-Disposition: form-data; name=`"file`"; filename=`"$fileName`"$crlf" +
        "Content-Type: application/octet-stream$crlf$crlf")
    $tail = [Text.Encoding]::UTF8.GetBytes("$crlf--$boundary--$crlf")
    $body = New-Object byte[] ($head.Length + $bytes.Length + $tail.Length)
    [Array]::Copy($head, 0, $body, 0, $head.Length)
    [Array]::Copy($bytes, 0, $body, $head.Length, $bytes.Length)
    [Array]::Copy($tail, 0, $body, $head.Length + $bytes.Length, $tail.Length)
    Invoke-RestMethod -Method Post `
        -Uri "$giteeApi/releases/$ReleaseId/attach_files?access_token=$GiteeToken" `
        -ContentType "multipart/form-data; boundary=$boundary" -Body $body `
        -TimeoutSec 600
}

function Remove-GiteeFile([int]$ReleaseId, [int]$AttachId) {
    Invoke-RestMethod -Method Delete `
        -Uri "$giteeApi/releases/$ReleaseId/attach_files/$AttachId`?access_token=$GiteeToken" `
        -TimeoutSec 60
}

function Publish-GiteeFile($Release, [string]$Path) {
    $releaseId = [int64](@($Release.id)[0])
    $name = Split-Path -Leaf $Path
    $localSize = (Get-Item $Path).Length
    foreach ($item in @(Get-GiteeAttachments $releaseId)) {
        if ($item.name -ne $name) { continue }
        if ([int64](@($item.size)[0]) -eq $localSize) {
            Write-Host "  已存在且大小一致，跳过 $name"
            return
        }
        # 大小不同说明本地产物更新过，删掉旧附件再传，避免客户端拿到旧文件。
        Write-Host "  替换同名附件 $name"
        Remove-GiteeFile -ReleaseId $releaseId -AttachId ([int64](@($item.id)[0]))
        break
    }
    Add-GiteeFile -ReleaseId $releaseId -Path $Path | Out-Null
    Write-Host "  已上传 $name（$([math]::Round($localSize / 1MB, 1)) MB）"
}

# ---------- 仓库初始化 ----------
# 空仓库没有分支时，Gitee 建发行版会因为 target_commitish 不存在而失败。
# 先用 contents API 放一个 README 创建默认分支，再读取真实分支名作为 commitish。
function Ensure-DepsBranch([string]$Preferred) {
    $want = if ($Preferred) { $Preferred } else { "master" }
    $branches = @()
    try {
        # 分支查询带上 token：空仓库匿名查询会直接返回空数组。
        $branches = @(Invoke-RestMethod -Uri "$giteeApi/branches?access_token=$GiteeToken" -TimeoutSec 60)
    } catch {
        $branches = @()
    }
    if ($branches.Count -gt 0) {
        $names = @($branches | ForEach-Object { [string](@($_.name)[0]) } | Where-Object { $_ })
        if ($want -and ($names -contains $want)) { return $want }
        return $names[0]
    }

    Write-Host "依赖仓库还没有分支，创建 README 初始化 $want 分支"
    $text = "# Paperead 依赖仓库`n`n首次启动后台下载的运行时与引擎物料，由 scripts/publish-deps.ps1 发布。`n"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))
    # 空仓库必须显式给 branch，否则 contents API 只会返回 {"message":"ref"} 而建不成文件。
    $created = Invoke-RestMethod -Method Post `
        -Uri "$giteeApi/contents/README.md?access_token=$GiteeToken" `
        -ContentType "application/json; charset=utf-8" `
        -Body (@{ content = $encoded; message = "init: 依赖仓库说明"; branch = $want } | ConvertTo-Json) `
        -TimeoutSec 120
    $sha = ""
    try { $sha = [string](@($created.commit.sha)[0]) } catch { $sha = "" }

    Start-Sleep -Seconds 2
    try {
        $branches = @(Invoke-RestMethod -Uri "$giteeApi/branches?access_token=$GiteeToken" -TimeoutSec 60)
    } catch {
        $branches = @()
    }
    if ($branches.Count -gt 0) {
        Write-Host "依赖仓库默认分支：$want"
        return $want
    }
    # 分支列表有延迟时，直接用刚创建的提交 sha 建发行版。
    if ($sha) {
        Write-Host "分支列表尚未就绪，改用提交 $sha 作为 target"
        return $sha
    }
    throw "依赖仓库初始化后仍没有分支，请先在网页创建 README 再重跑。"
}

# ---------- 创建/复用发行版 ----------
$rel = Get-GiteeRelease $Tag
if (-not $rel) {
    Write-Host "创建依赖发行版 $Tag"
    $target = Ensure-DepsBranch -Preferred $Commitish
    $rel = New-GiteeRelease -TagName $Tag -Name "依赖组件" `
        -Body "Paperead 运行依赖（首次启动后台下载）。含 BabelDOC 翻译引擎分卷、node 运行时与 pandoc；清单内带 sha256，客户端按哈希增量校验。" `
        -TargetCommitish $target
} else {
    Write-Host "依赖发行版 $Tag 已存在，补齐/更新附件"
}

foreach ($f in $files) { Publish-GiteeFile $rel $f.FullName }

# ---------- 下载自检 ----------
Write-Host ""
Write-Host "==== 下载自检 ===="
$failed = 0
foreach ($f in $files) {
    $name = $f.Name
    $url = "https://gitee.com/$GiteeRepo/releases/download/$Tag/$name"
    try {
        $head = Invoke-WebRequest -Uri $url -Method Head -MaximumRedirection 5 -UseBasicParsing -TimeoutSec 60
        if ($head.StatusCode -eq 200) {
            Write-Host "OK   $name"
        } else {
            Write-Host "FAIL $name（HTTP $($head.StatusCode)）" -ForegroundColor Red
            $failed++
        }
    } catch {
        Write-Host "FAIL $name（$($_.Exception.Message)）" -ForegroundColor Red
        $failed++
    }
}

if ($VerifyHash) {
    # 系统 %TEMP% 在本机可能不可写；校验文件放到项目内临时目录，用完清理。
    $tmpRoot = Join-Path $root ".publish-tmp"
    New-Item -ItemType Directory -Force -Path $tmpRoot | Out-Null
    foreach ($f in $files) {
        $name = $f.Name
        $url = "https://gitee.com/$GiteeRepo/releases/download/$Tag/$name"
        $tmp = Join-Path $tmpRoot ("deps-verify-" + $name)
        try {
            Invoke-WebRequest -Uri $url -OutFile $tmp -MaximumRedirection 5 -UseBasicParsing -TimeoutSec 1800
            $remote = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
            $local = (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower()
            if ($remote -ne $local) {
                Write-Host "FAIL $name 哈希不一致" -ForegroundColor Red
                $failed++
            } else {
                Write-Host "OK   $name 哈希一致"
            }
        } catch {
            Write-Host "FAIL $name 下载校验失败（$($_.Exception.Message)）" -ForegroundColor Red
            $failed++
        } finally {
            if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        }
    }
    Remove-Item $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($failed -gt 0) {
    throw "依赖发布自检失败 $failed 项"
}

Write-Host "依赖发布完成：$GiteeRepo@$Tag" -ForegroundColor Green
Write-Host "记得把客户端依赖源指向该仓库：PAPEREAD_DEPS_REPO=$GiteeRepo" -ForegroundColor Yellow