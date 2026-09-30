<#
一键启动：批量检测 API（127.0.0.1:8000）+ 监测平台（127.0.0.1:8010）。

  pwsh -File scripts\start_all.ps1

脚本会复用或创建 .venv、按需安装依赖、在缺少 .env 时从 .env.example 生成，
然后把进程 PID 写入 .run\ 目录，可用 scripts\stop_all.ps1 停止。
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host '[1/4] 未找到 .venv，正在创建…'
    python -m venv .venv
}
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }

Write-Host '[2/4] 检查依赖…'
& $python -c "import fastapi, httpx, pydantic, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '      正在安装 requirements.txt…'
    & $python -m pip install -r requirements.txt
}

$envFile = Join-Path $root '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination $envFile
    Write-Host '[3/4] 已从 .env.example 生成 .env，请按需修改令牌与模型配置'
} else {
    Write-Host '[3/4] 使用已有 .env'
}

$runDir = Join-Path $root '.run'
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

Write-Host '[4/4] 启动服务…'
$api = Start-Process -FilePath $python `
    -ArgumentList @('-B', '-m', 'uvicorn', 'app.api:app', '--host', '127.0.0.1', '--port', '8000', '--workers', '1') `
    -WorkingDirectory $root -PassThru
$api.Id | Set-Content -LiteralPath (Join-Path $runDir 'api.pid')
Start-Sleep -Seconds 2

$monitor = Start-Process -FilePath $python `
    -ArgumentList @('-B', '-m', 'monitor', '--host', '127.0.0.1', '--port', '8010') `
    -WorkingDirectory $root -PassThru
$monitor.Id | Set-Content -LiteralPath (Join-Path $runDir 'monitor.pid')

Write-Host ''
Write-Host "批量检测 API : http://127.0.0.1:8000/docs   (PID $($api.Id))"
Write-Host "监测平台     : http://127.0.0.1:8010/docs   (PID $($monitor.Id))"
Write-Host '停止服务     : pwsh -File scripts\stop_all.ps1'