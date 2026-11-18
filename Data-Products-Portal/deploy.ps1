# Data Products Portal — Azure Databricks Apps Deployment
# Run from the Data-Products-Portal\ directory: .\deploy.ps1 [-Target dev|prod]

param(
    [string]$Target = "dev"
)

Write-Host "=== Data Products Portal - Azure Databricks Apps Deployment ===" -ForegroundColor Cyan
Write-Host "Target : $Target"
Write-Host ""

Write-Host "[1/3] Building portal..." -ForegroundColor Yellow
npm run build
if (-not $?) { Write-Error "Portal build failed"; exit 1 }

Write-Host "[2/3] Validating Databricks Asset Bundle..." -ForegroundColor Yellow
databricks bundle validate -t $Target
if (-not $?) { Write-Error "Bundle validation failed"; exit 1 }

Write-Host "[3/3] Deploying to Azure Databricks Apps..." -ForegroundColor Yellow
databricks bundle deploy -t $Target
if (-not $?) { Write-Error "Bundle deploy failed"; exit 1 }

databricks bundle run data_products_portal -t $Target
