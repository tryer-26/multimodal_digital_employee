<#
.SYNOPSIS
    MDE-LAB 多模态数字员工平台 —— 停止脚本（Windows / PowerShell）。

.DESCRIPTION
    结束占用中台(8000) 与 前端(3000) 端口及其子进程。
    对 uvicorn --reload 场景会自动向上找到父进程，避免只杀掉工作子进程后
    重载器仍驻留。

.EXAMPLE
    .\scripts\stop.ps1
#>
param(
    [int[]]$Ports = @(8000, 3000)
)

$ErrorActionPreference = 'SilentlyContinue'

function Stop-Port {
    param([int]$Port)

    # 无监听时 Get-NetTCPConnection 会抛 CIM 查询异常，此处静默吞掉
    $owner = $null
    try {
        $owner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1 -ExpandProperty OwningProcess
    }
    catch {
        $owner = $null
    }
    if (-not $owner) {
        Write-Host "端口 $Port 无监听进程。" -ForegroundColor DarkGray
        return
    }

    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
    $target = $owner

    # uvicorn --reload 的监听进程是工作子进程，向上找父进程一并结束
    if ($proc) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)"
        if ($parent -and $parent.Name -match '^(python|pythonw|node|cmd)') {
            $target = $parent.ProcessId
        }
    }

    taskkill /PID $target /T /F | Out-Null
    Write-Host "已停止端口 $Port（PID $target 及其子进程）。" -ForegroundColor Green
}

foreach ($port in $Ports) {
    Stop-Port -Port $port
}
