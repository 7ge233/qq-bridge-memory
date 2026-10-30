#requires -Version 5.1
<#
  qq-bridge 记忆中间件补丁 —— 安装脚本

  用法：
    .\apply.ps1 -BridgeDir <qq-bridge 仓库路径>
    .\apply.ps1 -Check                 # 只检查能不能装，不改任何文件
    # 也可以先 set QQ_BRIDGE_DIR=<路径>，然后省略 -BridgeDir

  做了什么：把同目录的 bridge-memory.patch 打到 qq-bridge（git apply），
  打完后跑 node --check 验证语法。失败会立刻报错，不会留下半成品。
#>
param(
    [string]$BridgeDir = $env:QQ_BRIDGE_DIR,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$patch = Join-Path $PSScriptRoot 'bridge-memory.patch'

function Fail([string]$msg) {
    Write-Host "✗ $msg" -ForegroundColor Red
    exit 1
}

if (-not $BridgeDir) { Fail '请用 -BridgeDir 指定 qq-bridge 仓库路径，或先设置环境变量 QQ_BRIDGE_DIR' }
if (-not (Test-Path $patch)) { Fail "找不到补丁文件：$patch" }
if (-not (Test-Path (Join-Path $BridgeDir 'src\bridge.js'))) { Fail "不是 qq-bridge 仓库（缺少 src\bridge.js）：$BridgeDir" }
if (-not (Test-Path (Join-Path $BridgeDir 'package.json'))) { Fail "不是 qq-bridge 仓库（缺少 package.json）：$BridgeDir" }

$bridgeJs = Join-Path $BridgeDir 'src\bridge.js'
if (Select-String -Path $bridgeJs -Pattern 'qq-memory-client' -Quiet) {
    Write-Host '= 看起来已经装过了（src\bridge.js 里已有 qq-memory-client 引用）。' -ForegroundColor Yellow
    Write-Host '  要重装：先 .\revert.ps1，再 .\apply.ps1'
    exit 0
}

Push-Location $BridgeDir
try {
    if (-not (Test-Path '.git')) { Fail "目标目录不是 git 仓库：$BridgeDir" }

    git apply --check --whitespace=nowarn $patch
    if ($LASTEXITCODE -ne 0) {
        Fail "补丁与当前代码不匹配（多半是上游改过同一处）。先 git pull 到最新，或手动对照 README 合并。"
    }

    if ($Check) {
        Write-Host '✓ 可以安装（本次 -Check 未改动任何文件）' -ForegroundColor Green
        exit 0
    }

    git apply --whitespace=nowarn $patch
    if ($LASTEXITCODE -ne 0) { Fail '应用补丁失败' }

    node --check 'src\bridge.js'
    if ($LASTEXITCODE -ne 0) {
        Write-Host '✗ 打完补丁语法检查没过，正在自动回滚…' -ForegroundColor Red
        git apply -R --whitespace=nowarn $patch
        Fail '已回滚。请把上面的报错发出来。'
    }

    Write-Host '✓ 已安装（3 个文件）' -ForegroundColor Green
    Write-Host ''
    Write-Host '下一步：'
    Write-Host '  1. 设环境变量（改 start.bat 顶部方块）：QQ_MEMORY=1、QQ_MEMORY_BASE=http://127.0.0.1:8787'
    Write-Host '     DSH 端口变了还要设 DSH_BASE_URL=http://127.0.0.1:<端口>'
    Write-Host '  2. 确认侧车在跑：curl http://127.0.0.1:8787/healthz'
    Write-Host '  3. 重启桥接，日志里应出现 "DSH 地址：…（来源：…）"'
    Write-Host '  4. 发一条消息，日志应出现 [memory] 写入成功；否则看 README-step4.md 的排障一节'
}
finally {
    Pop-Location
}
