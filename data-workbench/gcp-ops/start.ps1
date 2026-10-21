# Start the VM and bring the Data Workbench stack up. Safe to re-run.
. "$PSScriptRoot\_config.ps1"

Write-Host "Starting VM $VM ..." -ForegroundColor Cyan
gcloud compute instances start $VM --zone=$ZONE | Out-Null

Write-Host "Waiting for SSH ..." -ForegroundColor Cyan
if (-not (Wait-ForSSH -TimeoutSec 240)) { Write-Host "VM did not become reachable in time." -ForegroundColor Red; exit 1 }

Write-Host "Bringing up containers (docker compose up -d) ..." -ForegroundColor Cyan
"y`n" | gcloud compute ssh $VM --zone=$ZONE --command="cd $APPDIR && sudo docker compose up -d 2>&1 | tail -4"

Write-Host ""
Write-Host "Data Workbench is starting. Open: $URL" -ForegroundColor Green
Write-Host "(Accept the self-signed cert, then sign in with Google / IAP.)"
Write-Host "Backend may take ~30-60s to report healthy. Run .\status.ps1 to check."
