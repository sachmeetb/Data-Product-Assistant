# Show VM power state, LB backend health, and container status.
. "$PSScriptRoot\_config.ps1"

Write-Host "=== VM ===" -ForegroundColor Cyan
gcloud compute instances describe $VM --zone=$ZONE --format="value(status)"

Write-Host "=== LB backend health ===" -ForegroundColor Cyan
gcloud compute backend-services get-health $BES --global --format="value(status.healthStatus[0].healthState)" 2>$null

Write-Host "=== Containers ===" -ForegroundColor Cyan
$state = gcloud compute instances describe $VM --zone=$ZONE --format="value(status)" 2>$null
if ($state -eq "RUNNING") {
    "y`n" | gcloud compute ssh $VM --zone=$ZONE --command="cd $APPDIR && sudo docker compose ps --format '{{.Name}}  {{.Status}}' 2>/dev/null; echo '--- health ---'; curl -s -o /dev/null -w 'frontend=%{http_code}\n' http://localhost:5173/ ; curl -s http://localhost:5173/api/health; echo"
} else {
    Write-Host "VM is $state - start it with .\start.ps1" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "URL: $URL" -ForegroundColor Green
