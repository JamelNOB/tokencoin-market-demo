[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envPath = Join-Path $projectRoot ".env"
$dataPath = Join-Path $projectRoot "backend\data"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python environment is missing. Run: python -m venv .venv"
}
if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env is missing. Copy backend/.env.example to .env and add your keys."
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "node_modules"))) {
    throw "Frontend dependencies are missing. Run: npm ci"
}

New-Item -ItemType Directory -Path $dataPath -Force | Out-Null

function Test-ListeningPort([int]$Port) {
    return $null -ne (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

if (-not (Test-ListeningPort 8000)) {
    Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $dataPath "backend.stdout.log") `
        -RedirectStandardError (Join-Path $dataPath "backend.stderr.log") | Out-Null
}

if (-not (Test-ListeningPort 5173)) {
    $npmPath = (Get-Command npm.cmd -ErrorAction Stop).Source
    Start-Process `
        -FilePath $npmPath `
        -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $dataPath "frontend.stdout.log") `
        -RedirectStandardError (Join-Path $dataPath "frontend.stderr.log") | Out-Null
}

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if ((Test-ListeningPort 8000) -and (Test-ListeningPort 5173)) {
        break
    }
    Start-Sleep -Milliseconds 250
}

if (-not (Test-ListeningPort 8000)) {
    throw "Backend did not start. Check backend/data/backend.stderr.log."
}
if (-not (Test-ListeningPort 5173)) {
    throw "Frontend did not start. Check backend/data/frontend.stderr.log."
}

Write-Host "TokenCoin is running:"
Write-Host "  UI:   http://127.0.0.1:5173"
Write-Host "  API:  http://127.0.0.1:8000/docs"
Write-Host "  Data: http://127.0.0.1:8000/api/status"
