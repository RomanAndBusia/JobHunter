$ErrorActionPreference = 'Stop'
$TaskRoot = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $TaskRoot 'backend'
$Frontend = Join-Path $TaskRoot 'frontend'
$Python = Join-Path $Backend '.venv\Scripts\python.exe'

Write-Host '[1/6] Checking Python 3.9...'
if (-not (Test-Path -LiteralPath $Python)) {
    py -3.9 -m venv (Join-Path $Backend '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.9 virtual environment creation failed. Install Python 3.9 and the py launcher.' }
}
$Version = (& $Python -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
if ($Version -ne '3.9') { throw "Expected Python 3.9, found $Version." }
Write-Host '[2/6] Installing pinned backend dependencies...'
& $Python -m pip install -r (Join-Path $Backend 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
Write-Host '[3/6] Checking installed Google Chrome...'
Push-Location $Backend
try { & $Python -c "from app.browser.chrome import find_chrome; print(find_chrome())"; if ($LASTEXITCODE -ne 0) { throw 'Install Google Chrome or configure JOBHUNTER_CHROME_PATH.' } } finally { Pop-Location }
Write-Host '[4/6] Installing frontend dependencies...'
Push-Location $Frontend
try { npm.cmd ci; if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' } } finally { Pop-Location }
Write-Host '[5/6] Preparing configuration and database...'
if (-not (Test-Path -LiteralPath (Join-Path $TaskRoot '.env'))) { Copy-Item -LiteralPath (Join-Path $TaskRoot '.env.example') -Destination (Join-Path $TaskRoot '.env') }
Push-Location $Backend
try { & $Python -m app.db.migrate; if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' } } finally { Pop-Location }
Write-Host '[6/6] Setup complete. Run start.bat for normal use.'
