#Requires -Version 5.1
<#
.SYNOPSIS
  Install and start the DSH Remote PC controlled-side service on this Windows PC.

.DESCRIPTION
  Copies the server and local control CLI, creates config.json/state.json with a
  fresh bearer token, registers logon autostart, opens a Tailscale-scoped
  firewall rule when elevated, starts the server, and generates the
  controller-side DSH bundle.

.PARAMETER InstallDir
  Application directory (default: %LOCALAPPDATA%\DSHRemotePC\app).

.PARAMETER DataDir
  Data directory for config/state/logs/screenshots (default:
  %LOCALAPPDATA%\DSHRemotePC\data).

.PARAMETER Port
  TCP port bound on loopback and the Tailscale address (default 8765).

.PARAMETER Bind
  tailscale (default), address, or all.

.PARAMETER PublicHost
  Hostname or IP to embed in the controller bundle and screenshot URLs. Default:
  the first Tailscale IPv4 address.

.PARAMETER RotateToken
  Generate a new bearer token (invalidates previously generated bundles).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1

.EXAMPLE
  .\deploy\install.ps1 -Port 8765 -RotateToken
#>
[CmdletBinding()]
param(
    [string] $InstallDir = (Join-Path $env:LOCALAPPDATA 'DSHRemotePC\app'),
    [string] $DataDir = (Join-Path $env:LOCALAPPDATA 'DSHRemotePC\data'),
    [int] $Port = 8765,
    [ValidateSet('tailscale', 'address', 'all')]
    [string] $Bind = 'tailscale',
    [string] $BindAddress = '',
    [string] $PublicHost = '',
    [string] $PythonPath = '',
    [switch] $RotateToken,
    [switch] $NoAutostart,
    [switch] $NoFirewall,
    [switch] $NoStart,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceRoot = Split-Path -Parent $scriptRoot
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$DataDir = [IO.Path]::GetFullPath($DataDir)
$taskName = 'DSHRemotePC'
$runValueName = 'DSHRemotePC'
$firewallPrefix = 'DSH Remote PC'

function Write-Step([string] $Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
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
    try {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($task -and $task.State -eq 'Running') {
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
Write-Host "DSH Remote PC installer" -ForegroundColor Green
Write-Host "  source : $sourceRoot"
Write-Host "  install: $InstallDir"
Write-Host "  data   : $DataDir"

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
    foreach ($folder in @('server', 'control', 'bundle', 'deploy')) {
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
[IO.File]::WriteAllText((Join-Path $InstallDir 'VERSION'), "dsh-pc-remote 1.0.0`r`n", (New-Object System.Text.UTF8Encoding($false)))
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
Write-Host "  token  : $($pairing.token)"
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
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
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
        throw 'The server did not become healthy within 10 seconds.'
    }
    Write-Host "  healthy on http://127.0.0.1:$Port/healthz"
}

Write-Step 'Generating the controller bundle'
$bundleScript = Join-Path $InstallDir 'deploy\make-bundle.ps1'
$bundleSource = Join-Path $InstallDir 'bundle'
$bundle = & $bundleScript -DataDir $DataDir -SourceBundle $bundleSource -Url $pairing.mcp_url -Force
Write-Host "  bundle : $($bundle.BundleDir)"
Write-Host "  zip    : $($bundle.ZipPath)"
Write-Host "  pairing: $($bundle.PairingPath)"

$controlScript = Join-Path $InstallDir 'control\dshpc.cmd'
$endpoints = @($pairing.urls) | ForEach-Object { "$_/mcp" }
Write-Step 'Installed'
Write-Host "  endpoints    : $($endpoints -join ', ')"
Write-Host "  serverName   : $($bundle.ServerName)"
Write-Host "  tool prefix  : mcp__$($bundle.ServerName)__"
Write-Host "  token        : $($pairing.token)"
Write-Host "  autostart    : $autostart"
Write-Host "  firewall     : $firewall"
Write-Host "  local control: `"$controlScript`" status"
Write-Host ''
Write-Host 'Next: copy the zip to the controller machine and install it as a DSH plugin.' -ForegroundColor Green
Write-Host 'See REMOTE-SETUP.md (or PAIRING.txt inside the bundle) for the exact steps.'
Write-Host ''
Write-Host 'Local control examples:' -ForegroundColor Green
Write-Host "  `"$controlScript`" status"
Write-Host "  `"$controlScript`" disable shell,screenshot"
Write-Host "  `"$controlScript`" all-off"
Write-Host "  `"$controlScript`" remote-control off"
Write-Host "  `"$controlScript`" sleep-hold --display"
