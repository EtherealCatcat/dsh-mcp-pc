#Requires -Version 5.1
<#
.SYNOPSIS
  Uninstall the DSH Remote PC controlled-side service from this Windows PC.

.DESCRIPTION
  Stops the server, removes logon autostart (scheduled task and/or Run key),
  removes the Tailscale firewall rule, and deletes the application directory.
  Configuration, logs, screenshots and the bearer token stay in the data
  directory unless -Purge is given.

  The controller-side gateway/pcs.json entry is not reachable from here; remove
  that entry on the controller after uninstalling.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\uninstall.ps1

.EXAMPLE
  .\deploy\uninstall.ps1 -Purge
#>
[CmdletBinding()]
param(
    [string] $InstallDir = (Join-Path $env:LOCALAPPDATA 'DSHRemotePC\app'),
    [string] $DataDir = (Join-Path $env:LOCALAPPDATA 'DSHRemotePC\data'),
    [switch] $Purge
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $env:SystemDrive
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$DataDir = [IO.Path]::GetFullPath($DataDir)
$taskName = 'DSHRemotePC'
$runValueName = 'DSHRemotePC'
$firewallPrefix = 'DSH Remote PC'

function Write-Step([string] $Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

Write-Host 'DSH Remote PC uninstaller' -ForegroundColor Green
Write-Host "  install: $InstallDir"
Write-Host "  data   : $DataDir"

Write-Step 'Removing logon autostart'
try {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "  removed scheduled task '$taskName'"
    } else {
        Write-Host '  no scheduled task'
    }
} catch {
    Write-Warning "Could not remove scheduled task: $($_.Exception.Message)"
}
try {
    Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
        -Name $runValueName -ErrorAction SilentlyContinue
} catch { }
Write-Host '  removed Run key (if present)'

Write-Step 'Stopping the server'
$pidPath = Join-Path $DataDir 'server.pid'
$stopped = $false
if (Test-Path -LiteralPath $pidPath) {
    try {
        $serverPid = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
        if (Get-Process -Id $serverPid -ErrorAction SilentlyContinue) {
            & taskkill.exe /PID $serverPid /T /F 2>$null | Out-Null
            Write-Host "  stopped pid $serverPid"
            $stopped = $true
        }
    } catch { }
}
try {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like '*dshpc_server.py*' -and
            $_.CommandLine -like "*$DataDir*"
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  stopped leftover pid $($_.ProcessId)"
            $stopped = $true
        }
} catch { }
if (-not $stopped) { Write-Host '  nothing running' }
try { Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue } catch { }

Write-Step 'Removing firewall rule'
try {
    $rules = Get-NetFirewallRule -DisplayName "$firewallPrefix*" -ErrorAction SilentlyContinue
    if ($rules) {
        $rules | Remove-NetFirewallRule -ErrorAction Stop
        Write-Host "  removed $($rules.Count) rule(s)"
    } else {
        Write-Host '  no rule found'
    }
} catch {
    Write-Warning "Could not remove the firewall rule. Run an elevated PowerShell: Get-NetFirewallRule -DisplayName '$firewallPrefix*' | Remove-NetFirewallRule"
}

Write-Step 'Removing application files'
if (Test-Path -LiteralPath $InstallDir) {
    try {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction Stop
        Write-Host "  removed $InstallDir"
    } catch {
        Write-Warning "Could not remove $InstallDir ($($_.Exception.Message)). Close any terminal using it and delete it manually."
    }
} else {
    Write-Host '  not present'
}

if ($Purge) {
    Write-Step 'Purging data (config, token, logs, screenshots)'
    if (Test-Path -LiteralPath $DataDir) {
        try {
            Remove-Item -LiteralPath $DataDir -Recurse -Force -ErrorAction Stop
            Write-Host "  removed $DataDir"
        } catch {
            Write-Warning "Could not remove $DataDir ($($_.Exception.Message))."
        }
    } else {
        Write-Host '  not present'
    }
    $parent = Split-Path -Parent $DataDir
    if ((Test-Path -LiteralPath $parent) -and -not (Get-ChildItem -LiteralPath $parent -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $parent -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Step 'Keeping data'
    Write-Host "  $DataDir was kept (token, logs, screenshots)."
    Write-Host '  To remove it too, re-run with -Purge.'
}

Write-Host ''
Write-Host 'Uninstalled. The PC no longer accepts remote DSH control.' -ForegroundColor Green
Write-Host 'On the controller, remove this PC from gateway/pcs.json and reload the dsh-pc-gateway plugin.'
