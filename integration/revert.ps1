#requires -Version 5.1
<#
  qq-bridge 记忆中间件补丁 —— 回滚脚本

  用法：
    .\revert.ps1 -BridgeDir <qq-bridge 路径>   # 回滚指定仓库
    # 也可以先 set QQ_BRIDGE_DIR=<路径>，然后省略 -BridgeDir
    .\revert.ps1 -BridgeDir D:\other   # 回滚别的仓库

  只回滚本补丁引入的改动（git apply -R）。如果那之后你又手改过这些文件，
  反向打补丁会失败，这时脚本会让你手动处理，不会强行覆盖你的改动。
#>
param(
    [string]$BridgeDir = $env:QQ_BRIDGE_DIR
)

$ErrorActionPreference = 'Stop'
$patch = Join-Path $PSScriptRoot 'bridge-memory.patch'

function Fail([string]$msg) {
    Write-Host "✗ $msg" -ForegroundColor Red
    exit 1
}

if (-not $BridgeDir) { Fail '请用 -BridgeDir 指定 qq-bridge 仓库路径，或先设置环境变量 QQ_BRIDGE_DIR' }
if (-not (Test-Path $patch)) { Fail "找不到补丁文件：$patch" }

$bridgeJs = Join-Path $BridgeDir 'src\bridge.js'
if (-not (Test-Path $bridgeJs)) { Fail "不是 qq-bridge 仓库：$BridgeDir" }

if (-not (Select-String -Path $bridgeJs -Pattern 'qq-memory-client' -Quiet)) {
    Write-Host '= 没有装（或已经回滚过），不需要操作。' -ForegroundColor Yellow
    exit 0
}

Push-Location $BridgeDir
try {
    git apply -R --check --whitespace=nowarn $patch 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '✗ 反向打补丁失败：这些文件在安装之后又被改过。' -ForegroundColor Red
        Write-Host '  手动回滚命令（会丢弃这些文件上所有未提交改动）：'
        Write-Host '    git checkout -- src/bridge.js start.bat'
        Write-Host '    Remove-Item src/qq-memory-client.js, ENV.md'
        exit 1
    }

    git apply -R --whitespace=nowarn $patch
    if ($LASTEXITCODE -ne 0) { Fail '回滚失败' }

    node --check 'src\bridge.js'
    if ($LASTEXITCODE -ne 0) { Fail '回滚后语法检查失败，请手动检查 src\bridge.js' }

    Write-Host '✓ 已回滚（src\bridge.js 恢复原状，客户端文件已删除）' -ForegroundColor Green
    Write-Host '  提醒：QQ_MEMORY 环境变量留着没影响——没有客户端代码时它什么都不做。'
}
finally {
    Pop-Location
}
