# Shared configuration for Data Workbench GCP ops scripts.
# Dot-sourced by the other scripts: . "$PSScriptRoot\_config.ps1"

$PROJECT   = "eogwapq-agbg-internal-data-mig"
$ZONE      = "us-central1-a"
$REGION    = "us-central1"
$VM        = "data-workbench"
$APPDIR    = "~/data-workbench"      # repo location on the VM (remote shell expands ~)
$LB_IP     = "136.68.242.223"
$HOSTNAME_ = "136.68.242.223.nip.io"       # nip.io name -> resolves to $LB_IP (IAP needs a hostname, not a bare IP)
$URL       = "https://136.68.242.223.nip.io/"

# Named GCP resources (for status / teardown)
$ADDRESS   = "wb-lb-ip"
$FR        = "wb-fr"                 # global forwarding rule
$PROXY     = "wb-https-proxy"        # target HTTPS proxy
$URLMAP    = "wb-um"
$BES       = "wb-bes"                # backend service
$CERT      = "wb-cert"               # original IP self-managed SSL cert (unused after nip.io switch)
$CERT2     = "wb-cert-nip"           # active self-managed SSL cert for the nip.io hostname
$HC        = "wb-hc"                 # health check
$IG        = "wb-ig"                 # unmanaged instance group
$FW_SSH    = "wb-allow-ssh-myip"
$FW_LB     = "wb-allow-lb-health"
$SUBNET    = "cmo-us-central1"
$SA        = "wb-vertex-sa@eogwapq-agbg-internal-data-mig.iam.gserviceaccount.com"

# Make gcloud always target the right project.
$env:CLOUDSDK_CORE_PROJECT = $PROJECT

function Wait-ForSSH {
    param([int]$TimeoutSec = 180)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $out = & { "y`n" | gcloud compute ssh $VM --zone=$ZONE --command="echo SSH_UP" 2>$null }
        if ($out -match "SSH_UP") { return $true }
        Start-Sleep -Seconds 8
    }
    return $false
}
