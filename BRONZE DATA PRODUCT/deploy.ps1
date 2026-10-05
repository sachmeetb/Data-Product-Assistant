# BFSI Bronze Agent — GCP Cloud Run Deployment (IAP + Proxy topology)
# Run from the BRONZE DATA PRODUCT\ directory: .\deploy.ps1
#
# Topology:
#   Browser → Proxy (IAP-gated, auth-proxy-sa) → Frontend (private) + Backend (private, /__api)
#   VITE_API_URL must be /__api so the proxy injects the backend service-account token.

$PROJECT_ID   = "eogwapq-agbg-internal-data-mig"
$REGION       = "us-central1"
$RUNTIME_SA   = "dp-assistant-sa@$PROJECT_ID.iam.gserviceaccount.com"
$PROXY_SA     = "auth-proxy-sa@$PROJECT_ID.iam.gserviceaccount.com"

$BACKEND_SVC  = "bfsi-bronze-backend-v1-1"
$FRONTEND_SVC = "preview-bfsi-bronze-frontend-v1-1"
$PROXY_SVC    = "proxy-bfsi-bronze-frontend-v1-1"
$REGISTRY     = "us-central1-docker.pkg.dev/$PROJECT_ID/bfsi-bronze"
$BACKEND_IMG  = "$REGISTRY/backend:v1.2"
$FRONTEND_IMG = "$REGISTRY/frontend:v1.2"

Write-Host "=== BFSI Bronze Agent — Cloud Run Deployment (IAP+Proxy) ===" -ForegroundColor Cyan
Write-Host "Project : $PROJECT_ID"
Write-Host "Region  : $REGION"
Write-Host ""

# ── 1. Build & push backend via Cloud Build ───────────────────────────────────
Write-Host "[1/5] Building backend with Cloud Build..." -ForegroundColor Yellow
gcloud builds submit . `
    --tag=$BACKEND_IMG `
    --project=$PROJECT_ID
if (-not $?) { Write-Error "Backend build failed"; exit 1 }

# ── 2. Deploy backend (PRIVATE — org policy blocks allUsers) ─────────────────
Write-Host "[2/5] Deploying private backend '$BACKEND_SVC'..." -ForegroundColor Yellow
gcloud run deploy $BACKEND_SVC `
    --image=$BACKEND_IMG `
    --region=$REGION `
    --platform=managed `
    --no-allow-unauthenticated `
    --service-account=$RUNTIME_SA `
    --memory=2Gi `
    --cpu=2 `
    --min-instances=0 `
    --max-instances=5 `
    --timeout=900 `
    --set-env-vars="GCP_PROJECT_ID=$PROJECT_ID,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION,GCP_LOCATION=$REGION,GOOGLE_GENAI_LOCATION=global,BQ_BRONZE_DATASET=banking_bronze,BQ_RAW_LANDING_PATH=gs://raw-landing-zone/,BQ_LOCATION=US,GEMINI_MODEL=gemini-2.5-flash,GEMINI_FLASH_MODEL=gemini-2.5-flash,GOOGLE_GENAI_USE_VERTEXAI=1,AGENT_APP_NAME=bfsi-bronze-agent,AGENT_MAX_CLARIFICATION_TURNS=3,AGENT_MAX_SPEC_ITERATIONS=3,AGENT_LOG_LEVEL=INFO,SESSION_BACKEND=memory,BQ_PUBLISHER_MODE=dry_run"
if (-not $?) { Write-Error "Backend deploy failed"; exit 1 }

$BACKEND_URL = (gcloud run services describe $BACKEND_SVC --region=$REGION --format="value(status.url)")
Write-Host "Backend live (private): $BACKEND_URL" -ForegroundColor Green

# ── 3. Build & push frontend (VITE_API_URL=/__api — proxy routes to backend) ──
# Do NOT bake the backend URL here; the browser calls /__api which the proxy
# resolves, adding a service-account token so the private backend accepts it.
Write-Host "[3/5] Building frontend with Cloud Build (VITE_API_URL=/__api)..." -ForegroundColor Yellow
gcloud builds submit ./frontend `
    --config=./frontend/cloudbuild.yaml `
    --substitutions="_VITE_API_URL=/__api,_IMAGE=$FRONTEND_IMG" `
    --project=$PROJECT_ID
if (-not $?) { Write-Error "Frontend build failed"; exit 1 }

# ── 4. Deploy frontend (PRIVATE) ──────────────────────────────────────────────
Write-Host "[4/5] Deploying private frontend '$FRONTEND_SVC'..." -ForegroundColor Yellow
gcloud run deploy $FRONTEND_SVC `
    --image=$FRONTEND_IMG `
    --region=$REGION `
    --platform=managed `
    --no-allow-unauthenticated `
    --service-account=$RUNTIME_SA `
    --memory=256Mi `
    --cpu=1 `
    --min-instances=0 `
    --max-instances=3 `
    --port=8080
if (-not $?) { Write-Error "Frontend deploy failed"; exit 1 }

$FRONTEND_URL = (gcloud run services describe $FRONTEND_SVC --region=$REGION --format="value(status.url)")
Write-Host "Frontend live (private): $FRONTEND_URL" -ForegroundColor Green

# ── 5. Grant proxy SA invoker rights + update proxy envs ─────────────────────
Write-Host "[5/5] Updating IAP proxy '$PROXY_SVC' — granting invoker & refreshing envs..." -ForegroundColor Yellow

# Idempotent: grant auth-proxy-sa Cloud Run invoker on both private services
gcloud run services add-iam-policy-binding $BACKEND_SVC `
    --region=$REGION `
    --member="serviceAccount:$PROXY_SA" `
    --role="roles/run.invoker" `
    --project=$PROJECT_ID

gcloud run services add-iam-policy-binding $FRONTEND_SVC `
    --region=$REGION `
    --member="serviceAccount:$PROXY_SA" `
    --role="roles/run.invoker" `
    --project=$PROJECT_ID

# Refresh proxy env to point at newly deployed revision URLs
gcloud run services update $PROXY_SVC `
    --region=$REGION `
    --platform=managed `
    --set-env-vars="FRONTEND_URL=$FRONTEND_URL,BACKEND_URL=$BACKEND_URL,API_PREFIX=/__api,SERVICE_NAME=bfsi-bronze" `
    --project=$PROJECT_ID
if (-not $?) { Write-Error "Proxy update failed"; exit 1 }

$PROXY_URL = (gcloud run services describe $PROXY_SVC --region=$REGION --format="value(status.url)")

Write-Host ""
Write-Host "=== Deployment Complete ===" -ForegroundColor Green
Write-Host "Proxy (IAP entry point) : $PROXY_URL"
Write-Host "Frontend (private)      : $FRONTEND_URL"
Write-Host "Backend  (private)      : $BACKEND_URL"
Write-Host ""
Write-Host "Users access via the Proxy URL above (IAP/Accenture SSO enforced)."
Write-Host "Health check requires a service-account token: $BACKEND_URL/health"
