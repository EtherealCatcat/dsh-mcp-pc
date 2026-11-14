#requires -Version 5.1
<#
.SYNOPSIS
  Local control for the DSH Remote PC controlled-side service.

.DESCRIPTION
  Thin wrapper around control\dshpc_cli.py. Works with or without the server
  running: feature switches live in the shared state.json that the server
  watches.

.EXAMPLE
  .\dshpc.ps1 status
  .\dshpc.ps1 disable shell,screenshot
  .\dshpc.ps1 all-off
  .\dshpc.ps1 sleep-hold --display
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Rest
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
$env:PYTHONIOENCODING = 'utf-8'

$python = $null
$pythonFile = Join-Path $root 'python-path.txt'
if (Test-Path -LiteralPath $pythonFile) {
    $candidate = (Get-Content -LiteralPath $pythonFile -Raw).Trim()
    if ($candidate -and (Test-Path -LiteralPath $candidate)) { $python = $candidate }
}
if (-not $python) {
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $python = $command.Source }
}
if (-not $python) {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { $python = $command.Source }
}
if (-not $python) {
    Write-Error 'Python 3 was not found. Re-run deploy\install.ps1 with -PythonPath, or install Python 3.10+.'
    exit 1
}

$cli = Join-Path $here 'dshpc_cli.py'
if (-not (Test-Path -LiteralPath $cli)) {
    Write-Error "CLI not found: $cli"
    exit 1
}

& $python $cli @Rest
exit $LASTEXITCODE
