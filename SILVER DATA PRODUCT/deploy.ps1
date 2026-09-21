# BFSI Silver Agent v1.1 — GCP Cloud Run Deployment Script (Cloud Build)
# Run from the project root: .\deploy.ps1

$PROJECT_ID     = "eogwapq-agbg-internal-data-mig"
$REGION         = "us-central1"
$BACKEND_SVC    = "bfsi-silver-backend-v1-1"
$FRONTEND_SVC   = "preview-bfsi-silver-frontend-v1-1"
$REGISTRY       = "us-central1-docker.pkg.dev/$PROJECT_ID/bfsi-silver"
$BACKEND_IMG    = "$REGISTRY/backend:v1.1"
$FRONTEND_IMG   = "$REGISTRY/frontend:v1.1"

Write-Host "=== BFSI Silver Agent v1.1 — Cloud Run Deployment ===" -ForegroundColor Cyan
Write-Host "Project : $PROJECT_ID"
Write-Host "Region  : $REGION"
Write-Host ""

# ── 1. Build & push backend via Cloud Build ───────────────────────────────────
Write-Host "[1/4] Building backend with Cloud Build..." -ForegroundColor Yellow
gcloud builds submit . `
    --tag=$BACKEND_IMG `
    --project=$PROJECT_ID
if (-not $?) { Write-Error "Backend build failed"; exit 1 }

# ── 2. Deploy backend to Cloud Run ────────────────────────────────────────────
Write-Host "[2/4] Deploying backend '$BACKEND_SVC'..." -ForegroundColor Yellow
gcloud run deploy $BACKEND_SVC `
    --image=$BACKEND_IMG `
    --region=$REGION `
    --platform=managed `
    --allow-unauthenticated `
    --memory=2Gi `
    --cpu=2 `
    --min-instances=0 `
    --max-instances=5 `
    --timeout=300 `
    --set-env-vars="GCP_PROJECT_ID=$PROJECT_ID,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION,GCP_LOCATION=$REGION,BQ_SILVER_DATASET=banking_silver,BQ_GOLD_DATASET=banking_gold,BQ_LOCATION=US,GEMINI_MODEL=gemini-2.5-flash,GOOGLE_GENAI_USE_VERTEXAI=1,AGENT_APP_NAME=bfsi-silver-agent,AGENT_MAX_CLARIFICATION_TURNS=3,AGENT_MAX_SPEC_ITERATIONS=3,AGENT_LOG_LEVEL=INFO,SESSION_BACKEND=memory,BQ_PUBLISHER_MODE=dry_run"
if (-not $?) { Write-Error "Backend deploy failed"; exit 1 }

$BACKEND_URL = (gcloud run services describe $BACKEND_SVC --region=$REGION --format="value(status.url)")
Write-Host "Backend live: $BACKEND_URL" -ForegroundColor Green

# ── 3. Build & push frontend via Cloud Build (bakes in backend URL) ───────────
Write-Host "[3/4] Building frontend with Cloud Build (VITE_API_URL=$BACKEND_URL)..." -ForegroundColor Yellow
gcloud builds submit ./frontend `
    --config=./frontend/cloudbuild.yaml `
    --substitutions="_VITE_API_URL=$BACKEND_URL,_IMAGE=$FRONTEND_IMG" `
    --project=$PROJECT_ID
if (-not $?) { Write-Error "Frontend build failed"; exit 1 }

# ── 4. Deploy frontend to Cloud Run ───────────────────────────────────────────
Write-Host "[4/4] Deploying frontend '$FRONTEND_SVC'..." -ForegroundColor Yellow
gcloud run deploy $FRONTEND_SVC `
    --image=$FRONTEND_IMG `
    --region=$REGION `
    --platform=managed `
    --allow-unauthenticated `
    --memory=256Mi `
    --cpu=1 `
    --min-instances=0 `
    --max-instances=3 `
    --port=8080
if (-not $?) { Write-Error "Frontend deploy failed"; exit 1 }

$FRONTEND_URL = (gcloud run services describe $FRONTEND_SVC --region=$REGION --format="value(status.url)")

Write-Host ""
Write-Host "=== Deployment Complete ===" -ForegroundColor Green
Write-Host "Backend  : $BACKEND_URL"
Write-Host "Frontend : $FRONTEND_URL"
Write-Host "Health   : $BACKEND_URL/health"
