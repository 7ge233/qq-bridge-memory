#requires -Version 5.1
<#
  重新生成补丁包（只有你改动过 bridge 的这几个文件时才需要跑）

  用法：
    .\pack.ps1                          # 默认仓库与基线 a49877f
    .\pack.ps1 -BridgeDir D:\other     # 指定仓库
    .\pack.ps1 -Base <commit>           # 指定基线 commit

  做的事：
    1. git diff <基线> → bridge-memory.patch（UTF-8 无 BOM，Linux 也能 git apply）
    2. 同步 qq-memory-client.js 副本
    3. 打印改动统计；随后用 .\apply.ps1 -Check 验证能打到干净基线上
#>
param(
    [string]$BridgeDir = $env:QQ_BRIDGE_DIR,
    [string]$Base = 'a49877f',
    [string[]]$Files = @('src/bridge.js', 'src/qq-memory-client.js', 'start.bat', 'ENV.md')
)

$ErrorActionPreference = 'Stop'
$out = Join-Path $PSScriptRoot 'bridge-memory.patch'
$clientSrc = Join-Path $BridgeDir 'src\qq-memory-client.js'
$clientDst = Join-Path $PSScriptRoot 'qq-memory-client.js'

function Fail([string]$msg) { Write-Host "✗ $msg" -ForegroundColor Red; exit 1 }

if (-not (Test-Path (Join-Path $BridgeDir 'src\bridge.js'))) { Fail "不是 qq-bridge 仓库：$BridgeDir" }
if (-not (Test-Path (Join-Path $BridgeDir '.git'))) { Fail "不是 git 仓库：$BridgeDir" }

Push-Location $BridgeDir
try {
    git cat-file -e "$Base^{commit}" 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "基线 commit 不存在：$Base" }

    $diff = git diff $Base -- $Files
    if ($LASTEXITCODE -ne 0) { Fail 'git diff 失败' }
    if (-not $diff) { Fail "基线 $Base 与当前工作区在这些文件上没有差异" }

    $text = ($diff -join [Environment]::NewLine) + [Environment]::NewLine
    $text = $text -replace "`r`n", "`n"   # 统一 LF：CRLF 的补丁在 Linux 上 apply 会失败
[System.IO.File]::WriteAllText($out, $text, (New-Object System.Text.UTF8Encoding($false)))

    if (Test-Path $clientSrc) {
        Copy-Item $clientSrc $clientDst -Force
        Write-Host '✓ 已同步 qq-memory-client.js 副本'
    }

    $lines = (Get-Content $out | Measure-Object -Line).Lines
    Write-Host "✓ 已生成 $out"
    Write-Host "  基线：$Base   行数：$lines"
    git diff --stat $Base -- $Files | ForEach-Object { Write-Host "  $_" }
    Write-Host ''
    Write-Host '下一步：.\apply.ps1 -Check'
}
finally {
    Pop-Location
}
