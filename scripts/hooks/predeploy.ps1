#!/usr/bin/env pwsh

[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

function Write-Color {
    param(
        [string]$Text,
        [ConsoleColor]$Color
    )
    Write-Host $Text -ForegroundColor $Color
}

Write-Host ""
Write-Color "📑 Loading environment variables from previous deployment (if available)…" Cyan
Write-Host ""

if (-not $Env:AZURE_APP_CONFIG_ENDPOINT) {
    Write-Color "⚠️  Skipping: AZURE_APP_CONFIG_ENDPOINT is not set." Yellow
} else {
    Write-Color "📦 Creating temporary virtual environment…" Blue
    python -m venv scripts/appconfig/.venv_temp

    # Activate the virtual environment
    $activateScript = "scripts/appconfig/.venv_temp/Scripts/Activate.ps1"
    if (Test-Path $activateScript) {
        . $activateScript
    } else {
        Write-Error "Activate script not found: $activateScript"
    }

    Write-Host ""
    Write-Color "⬇️  Installing requirements…" Blue
    pip install --upgrade pip
    pip install -r scripts/appconfig/requirements.txt

    Write-Host ""
    Write-Color "🚀 Running loadconfig.py…" Blue
    python -m scripts.appconfig.loadconfig
    Write-Color "✅ Environment variables loaded from App Configuration." Green

    Write-Host ""
    Write-Color "🛢️ Defining TAG value..." Blue
    if (-not $Env:TAG) {
        $TAG = git rev-parse --short HEAD
    } else {
        $TAG = $Env:TAG
    }
    azd env set TAG $TAG
    Write-Color "✅ TAG name set as: $TAG" Green

    # Retrieve environment values from azd
    $envValues = azd env get-values
    $ACRName    = ($envValues | Select-String '^AZURE_CONTAINER_REGISTRY_NAME=').Line.Split('=')[1].Trim('"')
    Write-Color "🛢️ ACR Name resolved from azd: $ACRName" Green

    $ACREndpoint = ($envValues | Select-String '^AZURE_CONTAINER_REGISTRY_ENDPOINT=').Line.Split('=')[1].Trim('"')
    Write-Color "🛢️ ACR Endpoint resolved from azd: $ACREndpoint" Green

    Write-Host ""
    Write-Color "🔐 Logging into ACR…" Green
    az acr login --name $ACRName

    $resourceGroup = ($envValues | Select-String '^AZURE_RESOURCE_GROUP=').Line.Split('=')[1].Trim('"')
    Write-Color "🛢️ Resource Group resolved from azd: $resourceGroup" Green

    $containerAppName = ($envValues | Select-String '^AZURE_FRONTEND_CONTAINER_APP_NAME=').Line.Split('=')[1].Trim('"')
    Write-Color "🛢️ Container app name resolved from azd: $containerAppName" Green

    Write-Host ""
    Write-Color "🔐 Associating ACR…" Green
    az containerapp registry set `
        --name $containerAppName `
        --resource-group $resourceGroup `
        --server $ACREndpoint `
        --identity system

    Write-Host ""
    Write-Color "🛠️  Building Docker image…" Green
    docker build -t "$ACREndpoint/azure-gpt-rag/frontend-build:$TAG" .

    Write-Host ""
    Write-Color "📤 Pushing image…" Green
    docker push "$ACREndpoint/azure-gpt-rag/frontend-build:$TAG"

    Write-Host ""
    Write-Color "🧹 Cleaning up…" Blue
    deactivate
    Remove-Item -Recurse -Force scripts/appconfig/.venv_temp
}