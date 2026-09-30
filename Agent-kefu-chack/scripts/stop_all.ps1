<#
停止 start_all.ps1 启动的服务。
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root '.run'

foreach ($name in @('api', 'monitor')) {
    $pidFile = Join-Path $runDir "$name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) { continue }
    $processId = Get-Content -LiteralPath $pidFile -Raw
    $processId = $processId.Trim()
    if ($processId) {
        try {
            Stop-Process -Id ([int]$processId) -Force -ErrorAction Stop
            Write-Host "已停止 $name (PID $processId)"
        } catch {
            Write-Host "$name (PID $processId) 未在运行"
        }
    }
    Remove-Item -LiteralPath $pidFile -Force
}