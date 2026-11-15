# Shared constants for install.ps1 / uninstall.ps1.
#
# Dot-source this file (". $PSScriptRoot\common.ps1"); never run it directly.
# Keeping the task/Run-key/firewall names in one place means uninstalling
# always removes exactly what installing created.

if (-not $env:LOCALAPPDATA) {
    throw 'LOCALAPPDATA is not set. Run this from a normal Windows user session.'
}

$DshpcDefaultInstallDir = Join-Path $env:LOCALAPPDATA 'DSHRemotePC\app'
$DshpcDefaultDataDir = Join-Path $env:LOCALAPPDATA 'DSHRemotePC\data'
$DshpcTaskName = 'DSHRemotePC'
$DshpcRunValueName = 'DSHRemotePC'
$DshpcFirewallPrefix = 'DSH Remote PC'
$DshpcDefaultPort = 8765
$DshpcHealthTimeoutSec = 10
$DshpcHealthPollMs = 250
