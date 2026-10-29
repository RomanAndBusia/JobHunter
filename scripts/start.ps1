$ErrorActionPreference = 'Stop'
$TaskRoot = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $TaskRoot 'backend'
$Frontend = Join-Path $TaskRoot 'frontend'
$Python = Join-Path $Backend '.venv\Scripts\python.exe'
$EnvFile = Join-Path $TaskRoot '.env'

function Get-ConfiguredPort([string]$Name, [int]$DefaultPort) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $DefaultPort }
    $Line = Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match "^$Name=" } | Select-Object -First 1
    if (-not $Line) { return $DefaultPort }
    $Value = ($Line -split '=', 2)[1].Trim()
    try { return ([uri]$Value).Port } catch { return $DefaultPort }
}

function Free-ListeningPort([int]$Port) {
    $Owners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    if (-not $Owners) { Write-Host "      $Port OK"; return }
    foreach ($OwnerPid in $Owners) {
        if ($OwnerPid -le 4) { throw "Port $Port is owned by protected system PID $OwnerPid." }
        Write-Host "      Port $Port occupied by PID $OwnerPid"
        Write-Host "      Stopping PID $OwnerPid..."
        Stop-Process -Id $OwnerPid -Force -ErrorAction Stop
    }
    for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
            Write-Host "      Port $Port released."
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Port $Port was not released in time."
}

function Wait-ListeningPort([int]$Port, [string]$Name) {
    for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name did not start listening on port $Port. Check storage\logs."
}

Write-Host '================================================'
Write-Host ' JobHunter'
Write-Host '================================================'
Write-Host '[1/5] Loading configuration...'
if (-not (Test-Path -LiteralPath $Python)) { throw 'backend\.venv is missing. Run setup.bat first.' }
$Version = (& $Python -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
if ($Version -ne '3.9') { throw "JobHunter requires Python 3.9; the project venv is Python $Version." }
if (-not (Test-Path -LiteralPath (Join-Path $Frontend 'node_modules'))) { throw 'frontend\node_modules is missing. Run setup.bat first.' }
$PreviousLocation = Get-Location
Set-Location $Backend
try { & $Python -m app.db.migrate; if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' } } finally { Set-Location $PreviousLocation }
$BackendPort = Get-ConfiguredPort 'JOBHUNTER_BACKEND_URL' 8000
$FrontendPort = Get-ConfiguredPort 'JOBHUNTER_FRONTEND_URL' 5173

Write-Host '[2/5] Freeing ports...'
Free-ListeningPort $BackendPort
Free-ListeningPort $FrontendPort
$LogFolder = Join-Path $TaskRoot 'storage\logs'
New-Item -ItemType Directory -Force -Path $LogFolder | Out-Null

Write-Host '[3/5] Browser worker: isolated diagnostics ready; runtime uses Proactor-safe backend entrypoint.'
Write-Host '[4/5] Starting backend...'
$BackendProcess = Start-Process -WindowStyle Hidden -PassThru -WorkingDirectory $Backend -FilePath $Python -ArgumentList @('-m', 'app.server') -RedirectStandardOutput (Join-Path $LogFolder 'backend.out.log') -RedirectStandardError (Join-Path $LogFolder 'backend.err.log')
Wait-ListeningPort $BackendPort 'Backend'

Write-Host '[5/5] Starting frontend...'
$FrontendProcess = Start-Process -WindowStyle Hidden -PassThru -WorkingDirectory $Frontend -FilePath 'npm.cmd' -ArgumentList @('run', 'dev', '--', '--port', "$FrontendPort") -RedirectStandardOutput (Join-Path $LogFolder 'frontend.out.log') -RedirectStandardError (Join-Path $LogFolder 'frontend.err.log')
Wait-ListeningPort $FrontendPort 'Frontend'

Write-Host ''
Write-Host "Backend:  http://127.0.0.1:$BackendPort"
Write-Host "Frontend: http://127.0.0.1:$FrontendPort"
Write-Host "Processes: backend $($BackendProcess.Id), frontend $($FrontendProcess.Id)"
Write-Host 'JobHunter started successfully.'
