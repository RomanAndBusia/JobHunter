$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Python = Join-Path $Backend '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Backend virtual environment is missing. Follow README.md setup first.'
}
if (-not (Test-Path -LiteralPath (Join-Path $Frontend 'node_modules'))) {
    throw 'Frontend dependencies are missing. Run npm install in frontend first.'
}

$backendProcess = Start-Process -WindowStyle Hidden -PassThru -WorkingDirectory $Backend -FilePath $Python -ArgumentList @('-m', 'app.server')
$frontendProcess = Start-Process -WindowStyle Hidden -PassThru -WorkingDirectory $Frontend -FilePath 'npm.cmd' -ArgumentList @('run', 'dev')

Write-Host "JobHunter backend PID: $($backendProcess.Id)"
Write-Host "JobHunter frontend PID: $($frontendProcess.Id)"
Write-Host 'Dashboard: http://127.0.0.1:5173'
Write-Host 'API docs: http://127.0.0.1:8000/docs'
Write-Host 'Use Stop-Process -Id <pid> to stop each development server.'
