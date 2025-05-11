#!/usr/bin/env pwsh
# ------------------------------------------------------------------------------
# predeploy.ps1 — validate env, optionally load App Config, then build & push
# ------------------------------------------------------------------------------

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Color constants
$Yellow = 'Yellow'
$Blue   = 'Cyan'
$Green  = 'Green'

Write-Host "`n🔍 Fetching all 'azd' environment values…" -ForegroundColor $Yellow
$envValues = azd env get-values

# Helper to extract a key from azd env output
function Get-EnvValue($key) {
    $pattern = "^$key=(.*)"
    foreach ($line in $envValues) {
        if ($line -match $pattern) { return ($Matches[1]).Trim('"') }
    }
    return ''
}

# Parse required values
$registryName        = Get-EnvValue 'AZURE_CONTAINER_REGISTRY_NAME'
$registryEndpoint    = Get-EnvValue 'AZURE_CONTAINER_REGISTRY_ENDPOINT'
$resourceGroup       = Get-EnvValue 'AZURE_RESOURCE_GROUP'
$appConfigEndpoint   = $Env:AZURE_APP_CONFIG_ENDPOINT

# Validate presence of all required variables
$missing = @()
if (-not $registryName)      { $missing += 'AZURE_CONTAINER_REGISTRY_NAME' }
if (-not $registryEndpoint)  { $missing += 'AZURE_CONTAINER_REGISTRY_ENDPOINT' }
if (-not $resourceGroup)     { $missing += 'AZURE_RESOURCE_GROUP' }
if (-not $appConfigEndpoint) { $missing += 'AZURE_APP_CONFIG_ENDPOINT' }

if ($missing.Count -gt 0) {
    Write-Host "`n⚠️  Missing required environment variables:" -ForegroundColor $Yellow
    foreach ($var in $missing) { Write-Host "    • $var" }
    Write-Host "`nPlease set them before running this script, e.g.:`n  azd env set <NAME> <VALUE>"
    exit 1
}

Write-Host "`n✅ All required azd env values are set." -ForegroundColor $Green

# Login to ACR
Write-Host "`n🔐 Logging into ACR ($registryName)…" -ForegroundColor $Green
az acr login --name $registryName

# Determine and set TAG
Write-Host "`n🛢️ Defining TAG…" -ForegroundColor $Blue
if (-not $Env:TAG) {
    $tag = (git rev-parse --short HEAD).Trim()
    azd env set TAG $tag
} else {
    $tag = $Env:TAG
}
Write-Host "✅ TAG set to: $tag" -ForegroundColor $Green

# Build Docker image
Write-Host "`n🛠️  Building Docker image…" -ForegroundColor $Green
docker build `
    --tag "$registryEndpoint/azure-gpt-rag/frontend-build:$tag" `
    .

# Push Docker image
Write-Host "`n📤 Pushing image…" -ForegroundColor $Green
docker push "$registryEndpoint/azure-gpt-rag/frontend-build:$tag"

# Validate and load App Config settings
Write-Host "`n🧩 Ensuring runtime settings are complete…" -ForegroundColor $Green
Write-Host "📦 Creating temporary virtual environment…" -ForegroundColor $Blue
python -m venv scripts/appconfig/.venv_temp

# Activate venv (PowerShell)
& scripts/appconfig/.venv_temp/Scripts/Activate.ps1

Write-Host "⬇️  Installing requirements…" -ForegroundColor $Blue
pip install --upgrade pip
pip install -r scripts/appconfig/requirements.txt

Write-Host "🚀 Running app_defaults.py…" -ForegroundColor $Blue
python -m scripts.appconfig.app_defaults
Write-Host "✅ Finished app settings validation." -ForegroundColor $Green

# Clean up virtual environment
Write-Host "`n🧹 Cleaning up…" -ForegroundColor $Blue
# Deactivate if available
if (Get-Command deactivate -ErrorAction SilentlyContinue) { deactivate }
Remove-Item -Recurse -Force scripts/appconfig/.venv_temp
