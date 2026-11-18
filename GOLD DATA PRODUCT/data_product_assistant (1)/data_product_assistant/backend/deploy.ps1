# Gold Data Product Assistant — Azure Databricks Apps Deployment
# Run from the backend\ directory: .\deploy.ps1 [-Target dev|prod]

param(
    [string]$Target = "dev"
)

Write-Host "=== Gold Data Product Assistant - Azure Databricks Apps Deployment ===" -ForegroundColor Cyan
Write-Host "Target : $Target"
Write-Host ""

Write-Host "[1/3] Building frontend..." -ForegroundColor Yellow
npm --prefix ..\frontend run build
if (-not $?) { Write-Error "Frontend build failed"; exit 1 }

Write-Host "[2/3] Validating Databricks Asset Bundle..." -ForegroundColor Yellow
databricks bundle validate -t $Target
if (-not $?) { Write-Error "Bundle validation failed"; exit 1 }

Write-Host "[3/3] Deploying to Azure Databricks Apps..." -ForegroundColor Yellow
databricks bundle deploy -t $Target
if (-not $?) { Write-Error "Bundle deploy failed"; exit 1 }

databricks bundle run gold_data_product -t $Target
