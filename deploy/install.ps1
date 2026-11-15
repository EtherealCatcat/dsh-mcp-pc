#Requires -Version 5.1
<#
.SYNOPSIS
  Install and start the DSH Remote PC controlled-side service on this Windows PC.

.DESCRIPTION
  Copies the server and local control CLI, creates config.json/state.json with a
  fresh bearer token, registers logon autostart, opens a Tailscale-scoped
  firewall rule when elevated, starts the server, and writes a gateway entry
  (gateway-entry.json) plus PAIRING.txt for the controller-side dsh-pc-gateway
  plugin.

.PARAMETER InstallDir
  Application directory (default: %LOCALAPPDATA%\DSHRemotePC\app).

.PARAMETER DataDir
  Data directory for config/state/logs/screenshots (default:
  %LOCALAPPDATA%\DSHRemotePC\data).

.PARAMETER Port
  TCP port bound on loopback and the Tailscale address (default 8765).

.PARAMETER Bind
  tailscale (default), address, or all.

.PARAMETER Alias
  Controller-side target name for this PC, matching [A-Za-z0-9_-]{1,16}.
  Default: the alias already published by a previous install, else a slug
  derived from the hostname. When omitted in an interactive console the
  installer asks for it; press Enter to accept the default in brackets.

.PARAMETER DisplayName
  Human-facing name shown next to the alias in controller tool descriptions.
  Default: the display name of a previous install, else the hostname. Also
  asked for interactively when omitted.

.PARAMETER Unattended
  Never prompt. Missing values fall back to their defaults. Use this for
  scripts, remote invocations, and other non-interactive runs; the installer
  detects a redirected stdin as non-interactive on its own and then behaves
  the same way.

.PARAMETER PublicHost
  Hostname or IP to embed in the gateway entry and screenshot URLs. Default:
  the first Tailscale IPv4 address.

.PARAMETER RotateToken
  Generate a new bearer token (the controller's existing pcs.json entry stops
  working until its token is updated).

.PARAMETER ShowToken
  Print the full bearer token once at the end. By default only the first and
  last four characters are shown; the real token stays in the pairing files.

.PARAMETER PairingCode
  Print the gateway entry as a single base64 line instead of showing the token
  (useful for copy/paste between machines). Mutually exclusive with
  -ShowToken; the encoded line is still a credential.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1

.EXAMPLE
  .\deploy\install.ps1 -Alias office -DisplayName 'Office PC'

.EXAMPLE
  .\deploy\install.ps1 -Unattended -Alias lab-vm -RotateToken

.EXAMPLE
  .\deploy\install.ps1 -PairingCode
#>
[CmdletBinding()]
param(
    [string] $InstallDir = '',
    [string] $DataDir = '',
    [int] $Port = 0,
    [ValidateSet('tailscale', 'address', 'all')]
    [string] $Bind = 'tailscale',
    [string] $BindAddress = '',
    [string] $PublicHost = '',
    [string] $Alias = '',
    [string] $DisplayName = '',
    [string] $PythonPath = '',
    [switch] $RotateToken,
    [switch] $Unattended,
    [switch] $ShowToken,
    [switch] $PairingCode,
    [switch] $NoAutostart,
    [switch] $NoFirewall,
    [switch] $NoStart,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptRoot 'common.ps1')
$sourceRoot = Split-Path -Parent $scriptRoot
if (-not $InstallDir) { $InstallDir = $DshpcDefaultInstallDir }
if (-not $DataDir) { $DataDir = $DshpcDefaultDataDir }
if (-not $Port -or $Port -le 0) { $Port = $DshpcDefaultPort }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$DataDir = [IO.Path]::GetFullPath($DataDir)
$taskName = $DshpcTaskName
$runValueName = $DshpcRunValueName
$firewallPrefix = $DshpcFirewallPrefix

function Write-Step([string] $Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-DefaultAlias([string] $Hostname) {
    $slug = ($Hostname.ToLowerInvariant() -replace '[^a-z0-9_-]+', '-').Trim('-')
    if (-not $slug) { $slug = 'pc' }
    if ($slug.Length -gt 16) { $slug = $slug.Substring(0, 16).Trim('-') }
    if (-not $slug) { $slug = 'pc' }
    return $slug
}

function Get-PythonPath([string] $Explicit) {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Explicit) { $candidates.Add($Explicit) }
    if ($env:DSHPC_PYTHON) { $candidates.Add($env:DSHPC_PYTHON) }
    foreach ($name in @('python.exe', 'python')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates.Add($command.Source) }
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $resolved = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($resolved -and (Test-Path -LiteralPath $resolved)) {
            return (Resolve-Path -LiteralPath $resolved).Path
        }
    }
    throw 'Python 3 was not found. Install Python 3.10+ (winget install Python.Python.3.12) or pass -PythonPath.'
}

function Get-ServerPid {
    $pidPath = Join-Path $DataDir 'server.pid'
    if (Test-Path -LiteralPath $pidPath) {
        try {
            $value = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
            if (Get-Process -Id $value -ErrorAction SilentlyContinue) { return $value }
        } catch { }
    }
    return 0
}

function Stop-Server {
    # Only touch the task when it actually serves this data directory; a
    # self-test or a second install must not kill an unrelated running server.
    try {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $taskArguments = ''
        if ($task) { $taskArguments = (@($task.Actions) | ForEach-Object { [string]$_.Arguments }) -join ' ' }
        if ($task -and $task.State -eq 'Running' -and $taskArguments -like "*$DataDir*") {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 300
        }
    } catch { }
    $serverPid = Get-ServerPid
    if ($serverPid -gt 0) {
        Write-Host "Stopping existing server (pid $serverPid)..."
        & taskkill.exe /PID $serverPid /T /F 2>$null | Out-Null
        Start-Sleep -Milliseconds 500
    }
}

function Register-Autostart {
    $pythonw = Join-Path (Split-Path $pythonExe) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $pythonExe }
    $serverPy = Join-Path $InstallDir 'server\dshpc_server.py'
    $arguments = '"{0}" --data-dir "{1}"' -f $serverPy, $DataDir
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

    # Prefer a logon scheduled task: it also restarts the server if it exits.
    try {
        $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -DontStopOnIdleEnd `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew `
            -StartWhenAvailable
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings `
            -Description 'DSH Remote PC controlled-side MCP server' -Force | Out-Null
        # Do not also start from Run, or the server would start twice.
        Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
            -Name $runValueName -ErrorAction SilentlyContinue
        return "Scheduled task '$taskName'"
    } catch {
        Write-Warning "Scheduled task registration failed ($($_.Exception.Message)); trying the per-user Run key instead."
        try {
            $value = '"{0}" "{1}" --data-dir "{2}"' -f $pythonw, $serverPy, $DataDir
            New-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Force | Out-Null
            Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
                -Name $runValueName -Value $value
            return 'HKCU Run key'
        } catch {
            Write-Warning "Run-key registration failed too ($($_.Exception.Message)). Autostart is NOT configured; start the server manually with 'dshpc start'."
            return 'NONE (autostart could not be registered)'
        }
    }
}

function Set-FirewallRule {
    if ($NoFirewall) { return 'skipped (-NoFirewall)' }
    $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        $command = "New-NetFirewallRule -DisplayName '$firewallPrefix ($Port)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress '100.64.0.0/10' -Profile Any"
        return "NOT CONFIGURED (run an elevated PowerShell: $command)"
    }
    Get-NetFirewallRule -DisplayName "$firewallPrefix*" -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    try {
        New-NetFirewallRule -DisplayName "$firewallPrefix ($Port)" `
            -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
            -RemoteAddress '100.64.0.0/10' -Profile Any `
            -Description 'DSH Remote PC MCP server, reachable only from the Tailscale CGNAT range' |
            Out-Null
        return "rule '$firewallPrefix ($Port)' allows 100.64.0.0/10 only"
    } catch {
        return "FAILED ($($_.Exception.Message)); add the rule manually"
    }
}

if ($env:OS -ne 'Windows_NT') { throw 'This installer is for Windows.' }
if ($ShowToken -and $PairingCode) {
    throw 'Choose either -ShowToken or -PairingCode, not both.'
}
$hostname = [System.Net.Dns]::GetHostName()

function Format-Token([string] $Value) {
    if (-not $Value) { return '(none)' }
    if ($Value.Length -le 10) { return '****' }
    return $Value.Substring(0, 4) + '****' + $Value.Substring($Value.Length - 4)
}

function Test-InteractiveConsole {
    if ($Unattended) { return $false }
    try { return [Environment]::UserInteractive -and -not [Console]::IsInputRedirected } catch { return $false }
}

# A reinstall keeps the alias and display name already published to the
# controller; deriving them from the hostname would silently rename the PC.
$existingEntryPath = Join-Path $DataDir 'gateway-entry.json'
$existingAlias = ''
$existingDisplayName = ''
if (Test-Path -LiteralPath $existingEntryPath) {
    try {
        $existingEntry = (Get-Content -LiteralPath $existingEntryPath -Raw) | ConvertFrom-Json
        if ($existingEntry.alias) { $existingAlias = [string]$existingEntry.alias }
        if ($existingEntry.displayName) { $existingDisplayName = [string]$existingEntry.displayName }
    } catch { }
}
$defaultAlias = $existingAlias.Trim()
if ($defaultAlias -notmatch '^[A-Za-z0-9_-]{1,16}$') { $defaultAlias = Get-DefaultAlias $hostname }
$defaultDisplayName = $existingDisplayName.Trim()
if (-not $defaultDisplayName) { $defaultDisplayName = $hostname }

$aliasName = $Alias.Trim()
if (-not $aliasName -and $env:DSHPC_ALIAS) { $aliasName = $env:DSHPC_ALIAS.Trim() }
$displayName = $DisplayName.Trim()
if (-not $displayName -and $env:DSHPC_DISPLAY_NAME) { $displayName = $env:DSHPC_DISPLAY_NAME.Trim() }
$interactive = Test-InteractiveConsole

Write-Host "DSH Remote PC installer" -ForegroundColor Green
Write-Host "  source : $sourceRoot"
Write-Host "  install: $InstallDir"
Write-Host "  data   : $DataDir"

if ($interactive -and (-not $aliasName -or -not $displayName)) {
    Write-Host ''
    Write-Host 'This PC has no -Alias / -DisplayName yet; press Enter to accept the bracketed default.' -ForegroundColor Cyan
}
if (-not $aliasName) {
    if ($interactive) {
        for ($attempt = 0; $attempt -lt 3 -and -not $aliasName; $attempt++) {
            $answer = (Read-Host "  alias [$defaultAlias]").Trim()
            if (-not $answer) { $aliasName = $defaultAlias; break }
            if ($answer -match '^[A-Za-z0-9_-]{1,16}$') { $aliasName = $answer; break }
            Write-Warning "alias '$answer' must match [A-Za-z0-9_-]{1,16}."
        }
    } else {
        $aliasName = $defaultAlias
    }
}
if (-not $displayName) {
    if ($interactive) {
        $answer = (Read-Host "  displayName [$defaultDisplayName]").Trim()
        if ($answer) { $displayName = $answer } else { $displayName = $defaultDisplayName }
    } else {
        $displayName = $defaultDisplayName
    }
}
$displayName = ($displayName -replace '[\x00-\x1F]', '').Trim()
if (-not $displayName) { $displayName = $defaultDisplayName }
if ($displayName.Length -gt 48) { $displayName = $displayName.Substring(0, 48).Trim() }
if (-not $aliasName) {
    throw 'No valid alias was chosen; pass -Alias ([A-Za-z0-9_-]{1,16}) or answer the prompt.'
}
if ($aliasName -notmatch '^[A-Za-z0-9_-]{1,16}$') {
    throw "Alias '$aliasName' must match [A-Za-z0-9_-]{1,16}."
}
Write-Host "  alias   : $aliasName"
Write-Host "  display : $displayName"

if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'server\dshpc_server.py'))) {
    throw "server sources not found under $sourceRoot (expected server\dshpc_server.py)"
}

Write-Step 'Checking Python'
$pythonExe = Get-PythonPath $PythonPath
$versionText = (& $pythonExe -c "import sys; print('%d.%d' % sys.version_info[:2])") -join ''
try { $pythonVersion = [version]$versionText } catch { throw "Could not read Python version from $pythonExe" }
if ($pythonVersion -lt [version]'3.10') {
    throw "Python 3.10+ is required; $pythonExe reports $versionText."
}
$bits = (& $pythonExe -c "import struct; print(struct.calcsize('P') * 8)") -join ''
Write-Host "  python : $pythonExe ($versionText, $bits-bit)"

Write-Step 'Stopping any previous instance'
Stop-Server

Write-Step 'Installing application files'
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
if ($sourceRoot -ne $InstallDir) {
    foreach ($folder in @('server', 'control', 'deploy')) {
        $destination = Join-Path $InstallDir $folder
        if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
        Copy-Item -LiteralPath (Join-Path $sourceRoot $folder) -Destination $destination -Recurse -Force
    }
    foreach ($file in @('README.md', 'REMOTE-SETUP.md', 'SECURITY.md')) {
        $source = Join-Path $sourceRoot $file
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $InstallDir -Force }
    }
} else {
    Write-Host '  already running from the install directory; skipping the copy step.'
}
$legacyBundle = Join-Path $InstallDir 'bundle'
if (Test-Path -LiteralPath $legacyBundle) {
    Remove-Item -LiteralPath $legacyBundle -Recurse -Force
    Write-Host '  removed legacy bundle/ template from the install directory.'
}
$serverVersion = (& $pythonExe -c "import sys; sys.path.insert(0, r'$($InstallDir)\server'); import dshpc_version; print(dshpc_version.__version__)" 2>$null | Select-Object -First 1)
if (-not $serverVersion) { throw 'Could not read the version from server\dshpc_version.py.' }
$serverVersion = "dsh-pc-remote $($serverVersion.Trim())"
[IO.File]::WriteAllText((Join-Path $InstallDir 'VERSION'), $serverVersion + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
[IO.File]::WriteAllText((Join-Path $InstallDir 'python-path.txt'), $pythonExe, (New-Object System.Text.UTF8Encoding($false)))

New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
$serverPy = Join-Path $InstallDir 'server\dshpc_server.py'

Write-Step 'Creating configuration (bearer token)'
$initArgs = @($serverPy, '--init', '--data-dir', $DataDir, '--port', $Port, '--bind', $Bind)
if ($BindAddress) { $initArgs += @('--bind-address', $BindAddress) }
if ($PublicHost) { $initArgs += @('--public-host', $PublicHost) }
if ($RotateToken) { $initArgs += '--rotate-token' }
$initOutput = (& $pythonExe @initArgs) -join "`n"
try { $pairing = $initOutput | ConvertFrom-Json } catch { throw "Could not parse --init output: $initOutput" }
if (-not $pairing.token) { throw 'The server did not return a bearer token.' }
Write-Host "  token  : $(Format-Token ([string]$pairing.token)) (masked; the full value is written to the pairing files)"
Write-Host "  mcp    : $($pairing.mcp_url)"

if (-not $NoAutostart) {
    Write-Step 'Registering logon autostart'
    $autostart = Register-Autostart
    Write-Host "  $autostart"
} else {
    $autostart = 'skipped (-NoAutostart)'
}

Write-Step 'Configuring Windows Firewall'
$firewall = Set-FirewallRule
Write-Host "  $firewall"

if (-not $NoStart) {
    Write-Step 'Starting the server'
    $pythonw = Join-Path (Split-Path $pythonExe) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $pythonExe }
    if ((-not $NoAutostart) -and ($autostart -like 'Scheduled task*')) {
        # Task Scheduler launches the process outside this console's job object,
        # so it keeps running after the installer returns.
        try {
            Start-ScheduledTask -TaskName $taskName
            Write-Host "  started scheduled task '$taskName'"
        } catch {
            Write-Warning "Could not start the scheduled task ($($_.Exception.Message)); starting directly instead."
            $startArgs = '"{0}" --data-dir "{1}"' -f $serverPy, $DataDir
            Start-Process -FilePath $pythonw -ArgumentList $startArgs -WindowStyle Hidden | Out-Null
        }
    } else {
        $startArgs = '"{0}" --data-dir "{1}"' -f $serverPy, $DataDir
        Start-Process -FilePath $pythonw -ArgumentList $startArgs -WindowStyle Hidden | Out-Null
    }
    $healthy = $false
    $attempts = [Math]::Max(1, [int]($DshpcHealthTimeoutSec * 1000 / $DshpcHealthPollMs))
    for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
        Start-Sleep -Milliseconds $DshpcHealthPollMs
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 1
            if ($response.StatusCode -eq 200) { $healthy = $true; break }
        } catch { }
    }
    if (-not $healthy) {
        $log = Join-Path $DataDir 'logs\server.log'
        if (Test-Path -LiteralPath $log) {
            Write-Host '  last log lines:' -ForegroundColor Yellow
            Get-Content -LiteralPath $log -Tail 15 -Encoding UTF8 | ForEach-Object { Write-Host "    $_" }
        }
        throw "The server did not become healthy within $DshpcHealthTimeoutSec seconds."
    }
    Write-Host "  healthy on http://127.0.0.1:$Port/healthz"
}

Write-Step 'Writing the gateway entry'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$gatewayEntryPath = Join-Path $DataDir 'gateway-entry.json'
$gatewayEntry = [ordered]@{
    alias = $aliasName
    displayName = $displayName
    url = [string]$pairing.mcp_url
    token = [string]$pairing.token
}
$gatewayEntryJson = $gatewayEntry | ConvertTo-Json -Depth 4
[IO.File]::WriteAllText($gatewayEntryPath, $gatewayEntryJson + "`r`n", $utf8)

$pairingLines = @(
    '=== DSH Remote PC pairing info / gateway entry ===',
    "host                 : $hostname",
    "alias                : $aliasName",
    "displayName          : $displayName",
    "MCP URL              : $($pairing.mcp_url)",
    "Bearer token         : $($pairing.token)",
    "entry                : $gatewayEntryPath",
    "created              : $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))",
    '',
    'usage:',
    '  1. Merge gateway-entry.json into the gateway/pcs.json pcs array on the controller (alias must be unique).',
    '  2. Reload the dsh-pc-gateway plugin (disable then enable, or restart DSH).',
    "  3. Call pc_list, then pc_sysinfo target=$aliasName to confirm the hostname.",
    '',
    'security:',
    '  - gateway-entry.json / PAIRING.txt contain the real bearer token; delete them after merging or store them as credentials.',
    '  - On this PC, dshpc all-off / dshpc remote-control off tighten permissions at any time.'
)
$pairingText = ($pairingLines -join "`r`n") + "`r`n"
$pairingPath = Join-Path $DataDir 'PAIRING.txt'
[IO.File]::WriteAllText($pairingPath, $pairingText, $utf8)
# Both files carry the bearer token, so keep them readable by this account only.
foreach ($secretPath in @($gatewayEntryPath, $pairingPath)) {
    & icacls.exe $secretPath /inheritance:r /grant:r "$($env:USERNAME):(F)" 2>$null | Out-Null
}

$controlScript = Join-Path $InstallDir 'control\dshpc.cmd'
$endpoints = @($pairing.urls) | ForEach-Object { "$_/mcp" }
$tokenPreview = Format-Token ([string]$pairing.token)

Write-Host ''
Write-Host 'Installed - the controlled-side setup on this PC is complete.' -ForegroundColor Green
Write-Host 'The service is running and autostarts at logon; nothing else is pending on this PC.' -ForegroundColor DarkGray
Write-Host ''
Write-Host "  host         : $hostname"
Write-Host "  alias        : $aliasName"
Write-Host "  displayName  : $displayName"
Write-Host "  endpoint     : $($pairing.mcp_url)"
Write-Host "  autostart    : $autostart"
Write-Host "  firewall     : $firewall"
Write-Host "  token        : $tokenPreview (full value lives in the pairing files below)"
Write-Host ''
Write-Host 'Pairing files (contain the real bearer token; delete them after merging):' -ForegroundColor Cyan
Write-Host "  $gatewayEntryPath"
Write-Host "  $pairingPath"
if ($ShowToken) {
    Write-Host ''
    Write-Host 'Full bearer token (printed because -ShowToken was passed):' -ForegroundColor Yellow
    Write-Host "  $($pairing.token)"
    Write-Host '  Do not copy this into a shared screen, recording, or chat transcript.' -ForegroundColor Yellow
}
if ($PairingCode) {
    $entryBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($gatewayEntryJson))
    Write-Host ''
    Write-Host 'Pairing code (base64 of gateway-entry.json; decode it into the controller entry):' -ForegroundColor Yellow
    Write-Host "  $entryBase64"
    Write-Host '  This line is a credential; treat it like the token itself.' -ForegroundColor Yellow
}
Write-Host ''
Write-Host 'Next step: register this PC on the controller.' -ForegroundColor Green
Write-Host '  1. Copy gateway-entry.json to the machine running DSH and merge it into gateway/pcs.json (pcs array, alias must be unique).'
Write-Host '  2. Reload the dsh-pc-gateway plugin (disable then enable, or restart DSH).'
Write-Host "  3. Call pc_list {probe:true}, then pc_sysinfo {target:$aliasName} to confirm the hostname."
Write-Host '  Re-print the full entry later (contains the token; avoid shared screens):'
Write-Host "    Get-Content `"$gatewayEntryPath`" -Raw"
Write-Host 'See REMOTE-SETUP.md for the exact steps.'
Write-Host ''
Write-Host 'Local control (optional):' -ForegroundColor Green
Write-Host "  `"$controlScript`" status"
Write-Host "  `"$controlScript`" disable shell,screenshot"
Write-Host "  `"$controlScript`" all-off"
Write-Host "  `"$controlScript`" remote-control off"
Write-Host "  `"$controlScript`" sleep-hold --display"
