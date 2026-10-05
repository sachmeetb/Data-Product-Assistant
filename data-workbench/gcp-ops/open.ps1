# Open the workbench in the default browser (starts the VM first if it is stopped).
. "$PSScriptRoot\_config.ps1"
$state = gcloud compute instances describe $VM --zone=$ZONE --format="value(status)" 2>$null
if ($state -ne "RUNNING") {
    Write-Host "VM is $state - starting it first ..." -ForegroundColor Yellow
    & "$PSScriptRoot\start.ps1"
}
Start-Process $URL
