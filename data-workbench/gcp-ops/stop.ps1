# Stop the VM to pause COMPUTE billing (~$264/mo while running -> $0 while stopped).
# Disk, load balancer, and reserved IP keep their small monthly charges (~$35/mo).
. "$PSScriptRoot\_config.ps1"

Write-Host "Stopping VM $VM (compute billing pauses; data on disk is preserved) ..." -ForegroundColor Cyan
gcloud compute instances stop $VM --zone=$ZONE | Out-Null
Write-Host "Stopped. Run .\start.ps1 to bring it back (app URL is unchanged: $URL)." -ForegroundColor Green
