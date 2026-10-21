# Open an interactive SSH session on the VM.
. "$PSScriptRoot\_config.ps1"
gcloud compute ssh $VM --zone=$ZONE
