<#
.SYNOPSIS
    MDE-LAB 多模态数字员工平台 —— 启动脚本（Windows / PowerShell）。

.DESCRIPTION
    启动中台（FastAPI，端口 8000）与前端（Vite，端口 3000）。
    -Target all（默认）：在新窗口分别启动两者，脚本立即返回；
    -Target backend / frontend：在当前窗口前台运行，Ctrl+C 停止。

    说明：中台以 --reload 模式运行，且仅监听 core/ 与 mcp_servers/ 下的 *.py；
    修改或新建 .env 不会触发热重载，需手动重启中台。

    -Target all 会先轮询中台 /api/health，就绪（最长等 60s）后再启动前端，
    避免 Vite 启动后立刻代理到尚未监听的 8000 端口而刷 ECONNREFUSED。
    中台输出在实时显示的同时会追加写入 logs/backend.log，便于事后排查。

.EXAMPLE
    .\scripts\start.ps1
    .\scripts\start.ps1 -Target backend
    .\scripts\start.ps1 -Target frontend
#>
param(
    [ValidateSet('all', 'backend', 'frontend')]
    [string]$Target = 'all'
)

$ErrorActionPreference = 'Stop'

# 项目根目录（scripts 的上一级）
$Root = Split-Path -Parent $PSScriptRoot

# 中台日志落盘位置：新窗口实时显示的同时，追加写入该文件
$LogDir = Join-Path $Root 'logs'
$BackendLog = Join-Path $LogDir 'backend.log'

function Initialize-Env {
    $envFile = Join-Path $Root '.env'
    if (-not (Test-Path $envFile)) {
        Copy-Item (Join-Path $Root '.env.example') $envFile
        Write-Warning '未找到 .env，已从 .env.example 复制；请填入 LLM_API_KEY / SILICONFLOW_API_KEY 后重启中台。'
    }
}

function Get-PythonExe {
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venvPython) { return $venvPython }
    Write-Warning '未找到 .venv 虚拟环境，回退使用系统 python。建议先执行：python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt'
    return 'python'
}

function Start-Backend {
    param([switch]$NewWindow)

    Initialize-Env
    $python = Get-PythonExe
    # --reload 默认监听整个工作目录（含 .venv 等），无关文件变更也会触发重载。
    # 用 --reload-dir 把监听范围收窄到项目自身代码目录，避免 .venv/data/logs 引发重载风暴。
    $cmdArgs = @('-m', 'uvicorn', 'core.main:app', '--host', '0.0.0.0', '--port', '8000',
        '--reload', '--reload-dir', 'core', '--reload-dir', 'mcp_servers')

    if ($NewWindow) {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
        # 新窗口里跑一条 PowerShell 包装命令，让 uvicorn 输出「实时显示 + 追加落盘」。
        # 两处必要处理，否则输出会乱：
        #   ① 统一 UTF-8：输出一旦被管道接管，Python 就不再走控制台 API，会退回系统
        #      默认编码（中文即 GBK），导致中文乱码、框线/emoji 被转义成 \u2580。
        #      故设 PYTHONIOENCODING=utf-8，并让 PowerShell 按 UTF-8 解码子进程输出。
        #   ② stderr 转字符串：2>&1 之后 PowerShell 会把首个 stderr 行渲染成红色
        #      NativeCommandError 错误块（其实只是 uvicorn 的正常 INFO），转成文本即可消除。
        $uvArgs = $cmdArgs -join ' '
        $inner = @(
            "`$env:PYTHONIOENCODING = 'utf-8'"
            '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)'
            "& '$python' $uvArgs 2>&1 | ForEach-Object { [string]`$_ } | Tee-Object -FilePath '$BackendLog' -Append"
        ) -join '; '
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile', '-Command', "`"$inner`"") `
            -WorkingDirectory $Root
    }
    else {
        Write-Host '中台启动中：http://localhost:8000 （Ctrl+C 停止）' -ForegroundColor Cyan
        & $python @cmdArgs
    }
}

function Wait-BackendReady {
    param([int]$TimeoutSeconds = 60)

    # 轮询 /api/health 直到中台返回 200：就绪返回 $true，超时返回 $false。
    # 用轮询替代固定 Start-Sleep，避免前端早于后端就绪而刷代理 ECONNREFUSED。
    # 注意：必须用 127.0.0.1 而非 localhost —— Windows 下 localhost 会先解析到 IPv6 ::1，
    # 而 uvicorn 只监听 IPv4（0.0.0.0），每次请求都要等 ::1 失败回退，实测约 2.1s，
    # 超过这里的 -TimeoutSec 会被判为超时，导致永远等不到就绪、白等满 60s 才起前端。
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -UseBasicParsing -TimeoutSec 3
            if ($resp.StatusCode -eq 200) { return $true }
        }
        catch {
            # 中台尚未监听时连接被拒属预期，忽略并继续轮询
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Start-Frontend {
    param([switch]$NewWindow)

    $feDir = Join-Path $Root 'frontend'
    if (-not (Test-Path (Join-Path $feDir 'node_modules'))) {
        Write-Host '首次运行，安装前端依赖...' -ForegroundColor Yellow
        Push-Location $feDir
        try { npm install } finally { Pop-Location }
    }

    if ($NewWindow) {
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', 'npm run dev' -WorkingDirectory $feDir
    }
    else {
        Write-Host '前端启动中：http://localhost:3000 （Ctrl+C 停止）' -ForegroundColor Cyan
        Push-Location $feDir
        try { npm run dev } finally { Pop-Location }
    }
}

switch ($Target) {
    'backend' { Start-Backend }
    'frontend' { Start-Frontend }
    'all' {
        Start-Backend -NewWindow
        if (Wait-BackendReady) {
            Write-Host '中台已就绪，正在启动前端...' -ForegroundColor Green
        }
        else {
            Write-Warning '等待中台就绪超时（60s），仍将启动前端；请查看 logs\backend.log 排查。'
        }
        Start-Frontend -NewWindow
        Write-Host '已在新窗口启动中台(8000) 与 前端(3000)。' -ForegroundColor Green
        Write-Host '中台日志：logs\backend.log（新窗口同时实时显示）'
        Write-Host '访问：http://localhost:3000/login （演示账号 user01 / agent01 / admin，密码 123456）'
        Write-Host '停止：.\scripts\stop.ps1'
    }
}
