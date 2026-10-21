# Data Workbench on GCP — operations

Deployment in project **eogwapq-agbg-internal-data-mig** (zone us-central1-a).
LLM: **Claude Sonnet 4.6 via Vertex AI** (VM authenticates as service account
`wb-vertex-sa`; no API key). Access is gated by **IAP** to five users.

## App URL (frontend + API, one origin)

    https://136.68.242.223.nip.io/

Use the **hostname**, not the bare IP — IAP requires a DNS name whose cert
matches (a bare IP triggers IAP "error code 52"). `136.68.242.223.nip.io` is a
public-DNS name that resolves to the LB IP `136.68.242.223`. If your corporate
network blocks `nip.io`, switch to an org-owned domain + managed cert instead.

The React frontend and its `/api` + `/ws` backend are served from this single
HTTPS address (nginx proxies `/api` and `/ws` to the backend container).
First visit: accept the self-signed certificate ("Advanced -> proceed"), then
sign in with Google (IAP). Authorized: lalit.menghani, michael.tremoulet,
r.yuva.kishore, sachmeet.bhatia, tony.d.giordano.

## Access it on demand

```powershell
cd gcp-ops
.\open.ps1        # starts the VM if stopped, then opens the URL
# or step by step:
.\start.ps1       # power on + bring containers up
.\status.ps1      # check VM / LB / container health
.\stop.ps1        # power off to pause compute billing (data is preserved)
```

Other helpers: `.\ssh.ps1` (shell on the VM), `.\logs.ps1` (tail backend logs;
`-Service frontend`/`neo4j`), `.\teardown.ps1` (delete everything).

## Change the Claude model

Enable the desired model in Vertex Model Garden (Console), then on the VM edit
`~/data-workbench/docker-compose.yml` backend env (`ANTHROPIC_MODEL` and the
three `ANTHROPIC_DEFAULT_*_MODEL` values), and `sudo docker compose up -d`.

## Teardown

```powershell
.\teardown.ps1                      # removes VM + load balancer + IP + firewall
.\teardown.ps1 -IncludeSharedInfra  # also removes the subnet + service account
```

## Monthly cost estimate (us-central1 list prices; E2 has no sustained-use discount)

Fixed infrastructure:

| Item | Running 24/7 | VM stopped (idle) |
|---|---|---|
| VM e2-highmem-8 (8 vCPU / 64 GB) | ~$264 | $0 |
| Boot disk 100 GB (pd-balanced) | ~$10 | ~$10 |
| Global HTTPS Load Balancer (forwarding rule) | ~$18 | ~$18 |
| External IPv4 (LB static + VM) | ~$6 | ~$4 |
| **Fixed subtotal** | **~$298/mo** | **~$32/mo** |

Plus **Vertex Claude Sonnet 4.6 usage** (variable): ~$3 / 1M input tokens,
~$15 / 1M output tokens (prompt caching lowers this). Charged only when agent
stages run.

Typical scenarios:
- **Left on 24/7:** ~$298/mo + Vertex usage.
- **~8 h/day (business hours, ~176 h/mo):** ~$100/mo + Vertex usage.
- **Stopped when idle:** ~$32/mo keeps everything provisioned (instant restart).
- **Torn down:** $0.

Estimates only — confirm against the GCP Pricing Calculator and your billing
console. Biggest lever: `.\stop.ps1` when you are not using it.
