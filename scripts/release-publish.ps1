# 发版收尾脚本：GitHub draft 转正 + Gitee 双发行版（版本版 + latest 固定版）+ 下载自检。
# 用法：
#   $env:GITEE_TOKEN = "私人令牌（需要 releases 权限）"
#   npm run release:publish -- -Version 0.1.7
# 版本号默认取 src-tauri\tauri.conf.json；物料从 release\ 读取。

param(
    [string]$Version = "",
    [string]$GiteeToken = $env:GITEE_TOKEN,
    [string]$GithubRepo = "xiaohui-plus/Paperead",
    [string]$GiteeRepo = "deng_xiaohui/paperead",
    [string]$SourceZip = "",
    [string]$Notes = "",
    [switch]$SkipGithub,
    [switch]$SkipGitee
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Version) {
    $Version = (Get-Content "src-tauri\tauri.conf.json" -Raw | ConvertFrom-Json).version
}
$tag = "v$Version"

$installer    = Join-Path $root "release\Paperead_${Version}_x64-setup.exe"
$installerZip = Join-Path $root "release\Paperead_${Version}_x64-setup.zip"
$pandoc       = Get-ChildItem (Join-Path $root "release\pandoc-*.zip") -ErrorAction SilentlyContinue | Select-Object -First 1
$latestJson   = Join-Path $root "release\latest.json"
$sums         = Join-Path $root "release\SHA256SUMS.txt"

# ---------- 前置检查 ----------
$missing = @()
foreach ($f in @($installer, $latestJson, $sums)) {
    if (-not (Test-Path $f)) { $missing += $f }
}
if (-not $pandoc) { $missing += "release\pandoc-*.zip" }
if ($missing.Count -gt 0) {
    throw "缺少发布物料：`n  " + ($missing -join "`n  ")
}

$manifest = Get-Content $latestJson -Raw | ConvertFrom-Json
if ($manifest.version -ne $Version) {
    throw "latest.json 版本 $($manifest.version) != $Version，先用 make_latest_json.py 重新生成"
}
if ("$($manifest.platforms.'windows-x86_64'.url)" -notmatch "gitee\.com") {
    Write-Host "WARN: latest.json 的下载 url 未指向 Gitee，国内用户更新会走慢通道" -ForegroundColor Yellow
}

Write-Host "发版版本：$tag"

# ---------- GitHub：draft 转正 ----------
$ghBody = ""
if (-not $SkipGithub) {
    function Get-GithubToken {
        if ($env:GH_TOKEN) { return $env:GH_TOKEN }
        $raw = "protocol=https`nhost=github.com`n" | git credential fill
        $m = $raw | Select-String "^password=(.+)$"
        if (-not $m) { throw "拿不到 GitHub token：先 gh auth login，或设置 GH_TOKEN" }
        return $m.Matches[0].Groups[1].Value
    }
    $tok = Get-GithubToken
    $gh = @{ Headers = @{ Authorization = "Bearer $tok"; Accept = "application/vnd.github+json" } }
    try {
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$GithubRepo/releases/tags/$tag" @gh
    } catch {
        throw "GitHub 上找不到 $tag（先推 tag 并等 CI 构建完）。$($_.Exception.Message)"
    }
    if ($rel.draft) {
        Invoke-RestMethod -Method Patch -Uri "https://api.github.com/repos/$GithubRepo/releases/$($rel.id)" @gh `
            -ContentType "application/json" -Body '{"draft":false,"prerelease":false,"make_latest":"true"}'
        Write-Host "GitHub: $tag 已转正并设为 latest"
    } else {
        Write-Host "GitHub: $tag 已是正式版，跳过"
    }
    $ghBody = $rel.body   # Gitee 描述直接复用，保证两边更新说明一字不差
}

# ---------- Gitee：版本发行版 + latest 固定发行版 ----------
if (-not $SkipGitee) {
    if (-not $GiteeToken) { throw "缺 GITEE_TOKEN（Gitee 私人令牌，需要 releases 权限）" }
    $giteeApi = "https://gitee.com/api/v5/repos/$GiteeRepo"

    function Get-GiteeRelease([string]$TagName) {
        try {
            return Invoke-RestMethod -Uri "$giteeApi/releases/tags/$TagName"
        } catch {
            $code = 0
            try { $code = [int]$_.Exception.Response.StatusCode } catch {}
            if ($code -eq 404) { return $null }
            throw "查询 Gitee 发行版 $TagName 失败（tag 推了吗？）：$($_.Exception.Message)"
        }
    }

    function New-GiteeRelease([string]$TagName, [string]$Name, [string]$Body) {
        Invoke-RestMethod -Method Post `
            -Uri "$giteeApi/releases?access_token=$GiteeToken" `
            -ContentType "application/json" `
            -Body (@{ tag_name = $TagName; name = $Name; body = $Body } | ConvertTo-Json)
    }

    function Add-GiteeFile([int]$ReleaseId, [string]$Path) {
        # PowerShell 5.1 没有 -Form，手工拼 multipart；字段名固定是 file
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
            -ContentType "multipart/form-data; boundary=$boundary" -Body $body
    }

    function Remove-GiteeFile([int]$ReleaseId, [int]$AttachId) {
        Invoke-RestMethod -Method Delete `
            -Uri "$giteeApi/releases/$ReleaseId/attach_files/$AttachId`?access_token=$GiteeToken"
    }

    function Publish-GiteeFile($Release, [string]$Path) {
        $name = Split-Path -Leaf $Path
        $old = @($Release.assets.attachments) | Where-Object { $_.name -eq $name } | Select-Object -First 1
        if ($old) {
            Write-Host "  替换同名附件 $name"
            Remove-GiteeFile -ReleaseId $Release.id -AttachId $old.id
        }
        Add-GiteeFile -ReleaseId $Release.id -Path $Path | Out-Null
        Write-Host "  已上传 $name"
    }

    $body = if ($ghBody) { $ghBody } elseif ($Notes) { $Notes } else { "Paperead $tag" }

    $rel = Get-GiteeRelease $tag
    if (-not $rel) {
        Write-Host "Gitee: 创建发行版 $tag"
        $rel = New-GiteeRelease -TagName $tag -Name $tag -Body $body
    } else {
        Write-Host "Gitee: 发行版 $tag 已存在，补齐附件（描述如需修改请手动编辑）"
    }

    $files = @($installer, $sums, $latestJson, $pandoc.FullName)
    if (Test-Path $installerZip) { $files += $installerZip }
    if ($SourceZip -and (Test-Path $SourceZip)) { $files += $SourceZip }
    else { Write-Host "  提示：未提供源码 zip（-SourceZip），跳过" }

    Write-Host "Gitee: 上传 $tag 附件"
    foreach ($f in $files) { Publish-GiteeFile $rel $f }

    # latest 固定发行版：应用更新检查的第一位端点，漏更新会导致已装用户检查不到新版本
    $latest = Get-GiteeRelease "latest"
    if (-not $latest) {
        Write-Host "Gitee: 首次创建 latest 固定发行版"
        $latest = New-GiteeRelease -TagName "latest" -Name "最新版本" `
            -Body "固定发行版：始终存放最新版的 latest.json，应用自动更新从这里检查。"
    }
    Write-Host "Gitee: 更新 latest 固定发行版的 latest.json"
    Publish-GiteeFile $latest $latestJson
}

# ---------- 下载自检 ----------
Write-Host ""
Write-Host "==== 下载自检 ===="
$checks = @(
    "https://gitee.com/$GiteeRepo/releases/download/$tag/$(Split-Path -Leaf $installer)",
    "https://gitee.com/$GiteeRepo/releases/download/latest/latest.json"
)
$failed = 0
foreach ($u in $checks) {
    try {
        $res = Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing
        if ($res.StatusCode -eq 200) { Write-Host "OK   $u" }
        else { Write-Host "FAIL $u （HTTP $($res.StatusCode)）" -ForegroundColor Red; $failed++ }
    } catch {
        Write-Host "FAIL $u （$($_.Exception.Message)）" -ForegroundColor Red
        $failed++
    }
}

try {
    $checkJson = Invoke-RestMethod -Uri $checks[1]
    if ($checkJson.version -ne $Version) {
        Write-Host "FAIL latest.json 内容版本 $($checkJson.version) != $Version" -ForegroundColor Red
        $failed++
    } else {
        Write-Host "OK   latest.json 版本 = $Version"
    }
} catch {
    Write-Host "FAIL latest.json 解析失败（$($_.Exception.Message)）" -ForegroundColor Red
    $failed++
}

if ($failed -gt 0) { exit 1 }
Write-Host ""
Write-Host "PUBLISH OK: $tag 已同步 GitHub + Gitee（版本版 + latest）"
