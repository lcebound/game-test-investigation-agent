param(
    [string]$ProviderConfig = "",
    [string]$LocalConfig = "data/local/workbench.env.json",
    [string]$SandboxConfig = "data/local/sandbox.env.json",
    [int]$Port = 8080
)

$ErrorActionPreference = 'Stop'
$agentRoot = Split-Path -Parent $PSScriptRoot
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
if ($LocalConfig) {
    $localPath = if ([IO.Path]::IsPathRooted($LocalConfig)) { $LocalConfig } else { Join-Path $agentRoot $LocalConfig }
    if (Test-Path -LiteralPath $localPath) {
        $jsonOptions = @{}
        if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
            $jsonOptions.DateKind = 'String'
        }
        $localValues = Get-Content -LiteralPath $localPath -Raw -Encoding UTF8 | ConvertFrom-Json @jsonOptions
        foreach ($entry in $localValues.PSObject.Properties) {
            if ($entry.Name -notmatch '^[A-Z][A-Z0-9_]{1,127}$' -or $entry.Value -isnot [string]) {
                throw 'Local configuration must map environment names to strings.'
            }
        }
        foreach ($entry in $localValues.PSObject.Properties) {
            if ($null -eq [Environment]::GetEnvironmentVariable($entry.Name, 'Process')) {
                [Environment]::SetEnvironmentVariable($entry.Name, $entry.Value, 'Process')
            }
        }
        $env:GTA_LOCAL_CONFIG_FILE = $localPath
    } elseif ($LocalConfig -ne 'data/local/workbench.env.json') {
        throw 'Local configuration file is missing.'
    }
}
if ($SandboxConfig) {
    $sandboxPath = if ([IO.Path]::IsPathRooted($SandboxConfig)) { $SandboxConfig } else { Join-Path $agentRoot $SandboxConfig }
    if (Test-Path -LiteralPath $sandboxPath) {
        $sandboxValues = Get-Content -LiteralPath $sandboxPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($entry in $sandboxValues.PSObject.Properties) {
            if ($entry.Name -notmatch '^GTA_SANDBOX_[A-Z0-9_]+$' -or $entry.Value -isnot [string]) {
                throw 'Sandbox configuration must contain GTA_SANDBOX_* string values.'
            }
            if ($null -eq [Environment]::GetEnvironmentVariable($entry.Name, 'Process')) {
                [Environment]::SetEnvironmentVariable($entry.Name, $entry.Value, 'Process')
            }
        }
    } elseif ($SandboxConfig -ne 'data/local/sandbox.env.json') {
        throw 'Sandbox configuration file is missing. Run scripts/setup_sandbox.py first.'
    }
}
if ($ProviderConfig) {
    $configPath = if ([IO.Path]::IsPathRooted($ProviderConfig)) {
        $ProviderConfig
    } else {
        Join-Path $agentRoot $ProviderConfig
    }
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (@($config.integration_providers | Where-Object { $null -ne $_ }).Count -gt 0) {
        throw 'HTTP API providers are no longer supported. Configure mcp_providers with the platform MCP endpoint.'
    }
    $mcp = @($config.mcp_providers | Where-Object { $null -ne $_ })
    foreach ($provider in $mcp) {
        if ($provider.token_env -and -not [Environment]::GetEnvironmentVariable($provider.token_env)) {
            throw "Missing service credential in environment variable $($provider.token_env). Inject the platform token into this process before starting the workbench."
        }
    }
    $env:GTA_MCP_PROVIDERS_JSON = ConvertTo-Json -InputObject $mcp -Depth 20 -Compress
}
$env:GTA_API_HOST = '127.0.0.1'
$env:GTA_API_PORT = [string]$Port
$env:PYTHONUTF8 = '1'
$sourcePath = Join-Path $agentRoot 'src'
$env:PYTHONPATH = if ($env:PYTHONPATH) {
    "$sourcePath$([IO.Path]::PathSeparator)$($env:PYTHONPATH)"
} else { $sourcePath }
$pythonPath = Join-Path $agentRoot '.venv/Scripts/python.exe'
Push-Location -LiteralPath $agentRoot
try {
    $approvalKeyCode = 'from game_test_agent.config import Settings; from game_test_agent.analysis.approval_keys import local_approval_key; print(local_approval_key(Settings.from_env()) or str())'
    $approvalLaunchKey = if (Test-Path -LiteralPath $pythonPath) {
        & $pythonPath -c $approvalKeyCode
    } else { & py -3 -c $approvalKeyCode }
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the local approval channel.' }
    $approvalEntry = if ($approvalLaunchKey) { '#approval_key=' + $approvalLaunchKey.Trim() } else { '' }
    Write-Host "Test workbench: http://127.0.0.1:$Port/$approvalEntry"
    if (Test-Path -LiteralPath $pythonPath) {
        & $pythonPath -m game_test_agent
    } else {
        & py -3 -m game_test_agent
    }
    if ($LASTEXITCODE -ne 0) { throw "Workbench process exited with code $LASTEXITCODE." }
} finally { Pop-Location }
