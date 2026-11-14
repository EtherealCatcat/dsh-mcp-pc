#Requires -Version 5.1
<#
.SYNOPSIS
  Generate the controller-side DSH bundle (and a zip) for this controlled PC.

.DESCRIPTION
  Reads the controlled-side config.json, fills the bundle's URL/token/name
  placeholders, writes PAIRING.txt, and produces a zip that can be copied to a
  controller machine and installed through DSH's Plugins UI or plugin_manager.

  Re-run this after changing the port, public host, or bearer token.
#>
[CmdletBinding()]
param(
    [string] $DataDir = (Join-Path $env:LOCALAPPDATA 'DSHRemotePC\data'),
    [string] $SourceBundle = '',
    [string] $OutputDir = '',
    [string] $Url = '',
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SourceBundle) { $SourceBundle = Join-Path (Split-Path -Parent $scriptRoot) 'bundle' }
$DataDir = [IO.Path]::GetFullPath($DataDir)
$SourceBundle = [IO.Path]::GetFullPath($SourceBundle)
if (-not $OutputDir) { $OutputDir = $DataDir }
$OutputDir = [IO.Path]::GetFullPath($OutputDir)

$configPath = Join-Path $DataDir 'config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "config.json not found: $configPath. Run deploy\install.ps1 first."
}
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$token = [string]$config.token
if (-not $token) { throw "config.json has no bearer token; run install.ps1 -RotateToken." }
$port = [int]$config.port

$hostname = [System.Net.Dns]::GetHostName()
$slug = ($hostname.ToLowerInvariant() -replace '[^a-z0-9]+', '-').Trim('-')
if (-not $slug) { $slug = 'pc' }
if ($slug.Length -gt 20) { $slug = $slug.Substring(0, 20).Trim('-') }
$serverName = 'dshpc_' + ($slug -replace '-', '_')
$packageName = 'dsh-remote-pc-' + $slug
$bundleId = 'mcp-dshpc-' + $slug

if (-not $Url) {
    $publicHost = [string]$config.public_host
    if ($publicHost) {
        if ($publicHost -match '^https?://') { $Url = $publicHost.TrimEnd('/') }
        else { $Url = "http://$publicHost`:$port" }
    }
    else {
        $tailscaleIp = ''
        try {
            $address = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
                Where-Object { $_.InterfaceAlias -match 'Tailscale' -and $_.IPAddress -like '100.*' } |
                Select-Object -First 1
            if ($address) { $tailscaleIp = [string]$address.IPAddress }
        } catch { }
        if (-not $tailscaleIp) {
            try {
                $cli = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
                if (Test-Path -LiteralPath $cli) {
                    $candidate = (& $cli ip -4 2>$null | Select-Object -First 1)
                    if ($candidate) { $tailscaleIp = $candidate.Trim() }
                }
            } catch { }
        }
        if (-not $tailscaleIp) {
            Write-Warning 'No Tailscale IPv4 address found. The generated bundle will point at 127.0.0.1 and will not reach this PC from another machine.'
            $tailscaleIp = '127.0.0.1'
        }
        $Url = "http://$tailscaleIp`:$port"
    }
}
$Url = $Url.TrimEnd('/')
if ($Url -notmatch '/mcp$') { $Url = $Url + '/mcp' }

$bundleDir = Join-Path $OutputDir 'controller-bundle'
if (Test-Path -LiteralPath $bundleDir) {
    Remove-Item -LiteralPath $bundleDir -Recurse -Force
}
New-Item -ItemType Directory -Path $bundleDir -Force | Out-Null
Copy-Item -Path (Join-Path $SourceBundle '*') -Destination $bundleDir -Recurse -Force

$utf8 = New-Object System.Text.UTF8Encoding($false)
function Update-Placeholders {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $text = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    $text = $text.Replace('__PACKAGE_NAME__', $packageName)
    $text = $text.Replace('__BUNDLE_ID__', $bundleId)
    $text = $text.Replace('__SERVER_NAME__', $serverName)
    $text = $text.Replace('__MCP_URL__', $Url)
    $text = $text.Replace('__TOKEN__', $token)
    [IO.File]::WriteAllText($Path, $text, $utf8)
}
Update-Placeholders (Join-Path $bundleDir 'package.json')
Update-Placeholders (Join-Path $bundleDir 'cordis.patch.yml')

$pairingLines = @(
    '=== DSH 远程电脑配对信息 / pairing ===',
    "被控端主机 / host : $hostname",
    "MCP 地址 / URL     : $Url",
    "Bearer token       : $token",
    "serverName         : $serverName",
    "工具前缀 / tools   : mcp__${serverName}__get_system_info",
    "生成时间 / created : $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))",
    '',
    '安装 / install:',
    '  1. 把本文件夹（或同名 zip）复制到控制端电脑。',
    '  2. DSH → 插件（Plugins）→ 添加插件（Add plugin）→ 选择本文件夹；',
    '     或让 DSH 用 plugin_manager 的 install_bundle 安装该绝对路径。',
    '  3. 新建会话或重载配置，让 MCP 客户端连接。',
    '  4. 让助手先调用 get_system_info 确认主机名。',
    '',
    '安全 / security:',
    '  - 本文件与 cordis.patch.yml 含真实 Bearer token，请勿公开或提交到仓库。',
    '  - 被控端可在本机运行 dshpc all-off / dshpc remote-control off 随时收紧权限。'
)
$pairing = ($pairingLines -join "`r`n") + "`r`n"
$pairingPath = Join-Path $bundleDir 'PAIRING.txt'
[IO.File]::WriteAllText($pairingPath, $pairing, $utf8)

$zipPath = Join-Path $OutputDir ("dsh-pc-remote-$slug.zip")
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
Compress-Archive -Path (Join-Path $bundleDir '*') -DestinationPath $zipPath -CompressionLevel Optimal

$result = [pscustomobject]@{
    Hostname    = $hostname
    Slug        = $slug
    ServerName  = $serverName
    PackageName = $packageName
    BundleId    = $bundleId
    McpUrl      = $Url
    Token       = $token
    BundleDir   = $bundleDir
    ZipPath     = $zipPath
    PairingPath = $pairingPath
}
$result
