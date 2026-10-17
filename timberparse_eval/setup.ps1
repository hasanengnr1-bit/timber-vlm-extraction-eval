param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$evaluationDirectory = $PSScriptRoot
& $Python -m venv (Join-Path $evaluationDirectory '.venv')
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
& (Join-Path $evaluationDirectory '.venv/Scripts/python.exe') -m pip install -r (Join-Path $evaluationDirectory 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host 'Ready. Run ./start.ps1, then open http://127.0.0.1:8765'
