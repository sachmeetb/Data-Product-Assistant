# Tail backend logs (Ctrl+C to stop). Pass a service name to tail a different one.
param([string]$Service = "backend", [int]$Tail = 120)
. "$PSScriptRoot\_config.ps1"
gcloud compute ssh $VM --zone=$ZONE --command="cd $APPDIR && sudo docker compose logs -f --tail=$Tail $Service"
