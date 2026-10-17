param([string]$Results = '', [int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$evaluationDirectory = $PSScriptRoot
$evaluationPython = Join-Path $evaluationDirectory '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $evaluationPython)) {
    throw 'Run setup.ps1 first to install the local environment.'
}
$evaluationArguments = @((Join-Path $evaluationDirectory 'server.py'), '--port', "$Port")
if ($Results) { $evaluationArguments += @('--results', $Results) }
& $evaluationPython @evaluationArguments
