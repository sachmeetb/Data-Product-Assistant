# Delete the Data Workbench GCP deployment.
# Default: removes all APP + LB resources (VM, LB, IP, firewall).
# -IncludeSharedInfra also removes the subnet, service account, and its IAM grant.
# Requires typing the confirmation phrase.
param([switch]$IncludeSharedInfra)
. "$PSScriptRoot\_config.ps1"

Write-Host "This will DELETE the Data Workbench deployment in project $PROJECT." -ForegroundColor Red
if ($IncludeSharedInfra) {
    Write-Host "INCLUDING shared infra: subnet $SUBNET, service account $SA, and its aiplatform.user grant." -ForegroundColor Red
}
$ans = Read-Host "Type 'DELETE' to proceed"
if ($ans -ne "DELETE") { Write-Host "Aborted."; exit 0 }

function Del($desc, $args) {
    Write-Host "-> $desc" -ForegroundColor Cyan
    & gcloud @args --quiet 2>$null
}

# Order matters: front of the LB first, then backends, then the VM.
Del "forwarding rule"        @("compute","forwarding-rules","delete",$FR,"--global")
Del "target HTTPS proxy"     @("compute","target-https-proxies","delete",$PROXY,"--global")
Del "URL map"                @("compute","url-maps","delete",$URLMAP,"--global")
Del "backend service"        @("compute","backend-services","delete",$BES,"--global")
Del "SSL certificate (nip)"  @("compute","ssl-certificates","delete",$CERT2,"--global")
Del "SSL certificate (ip)"   @("compute","ssl-certificates","delete",$CERT,"--global")
Del "health check"           @("compute","health-checks","delete",$HC)
Del "instance group"         @("compute","instance-groups","unmanaged","delete",$IG,"--zone",$ZONE)
Del "VM instance"            @("compute","instances","delete",$VM,"--zone",$ZONE)
Del "firewall (ssh)"         @("compute","firewall-rules","delete",$FW_SSH)
Del "firewall (lb)"          @("compute","firewall-rules","delete",$FW_LB)
Del "static IP"              @("compute","addresses","delete",$ADDRESS,"--global")

if ($IncludeSharedInfra) {
    Del "subnet"                 @("compute","networks","subnets","delete",$SUBNET,"--region",$REGION)
    Write-Host "-> removing IAM grant + service account" -ForegroundColor Cyan
    gcloud projects remove-iam-policy-binding $PROJECT --member="serviceAccount:$SA" --role="roles/aiplatform.user" --quiet 2>$null
    gcloud iam service-accounts delete $SA --quiet 2>$null
}

Write-Host ""
Write-Host "Teardown complete." -ForegroundColor Green
if (-not $IncludeSharedInfra) {
    Write-Host "Kept shared infra (subnet/SA). Re-run with -IncludeSharedInfra to remove those too." -ForegroundColor Yellow
}
