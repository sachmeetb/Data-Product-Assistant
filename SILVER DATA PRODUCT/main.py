"""
main.py — Application entry point for BFSI-Silver-Agent.

Usage:
  # Start the REST API server:
  python main.py server

  # Run the pipeline interactively (CLI):
  python main.py pipeline

  # Run pipeline from a JSON spec file:
  python main.py pipeline tests/sample_requirements.json

  # Validate the environment configuration:
  python main.py validate-env
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).parent
load_dotenv(_ROOT / ".env")

# ── SSL bundle patch (LOCAL DEV ONLY) ────────────────────────────────────────
# google-auth creates its own requests.Session internally and does not always
# pick up REQUESTS_CA_BUNDLE from the environment at runtime.
# We patch certifi.where() + force env vars with the ABSOLUTE bundle path so
# every SSL connection (requests, urllib3, google-auth) uses our bundle.
# On GCP this block is harmless — the bundle file won't exist and the fallback
# to the system default is safe (GCP uses the internal metadata server anyway).
_SSL_BUNDLE = _ROOT / "certs" / "google-ca-bundle.pem"
if _SSL_BUNDLE.is_file():
    import certifi
    _bundle_str = str(_SSL_BUNDLE)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle_str)
    os.environ.setdefault("SSL_CERT_FILE", _bundle_str)
    # Monkey-patch certifi.where() so google-genai / google-auth always find it
    certifi.where = lambda: _bundle_str
# ─────────────────────────────────────────────────────────────────────────────

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)


def _check_env() -> list[str]:
    """Return list of missing/empty required environment variables."""
    required = ["GCP_PROJECT_ID", "GEMINI_MODEL"]
    missing = []
    for var in required:
        val = os.environ.get(var, "").strip()
        if not val or val.startswith("your-"):
            missing.append(var)
    return missing


def cmd_validate_env():
    """Print environment configuration status."""
    print("\n=== BFSI-Silver-Agent — Environment Validation ===\n")
    checks = {
        "GCP_PROJECT_ID":      os.environ.get("GCP_PROJECT_ID", ""),
        "GCP_LOCATION":        os.environ.get("GCP_LOCATION", ""),
        "GEMINI_MODEL":        os.environ.get("GEMINI_MODEL", ""),
        "BQ_SILVER_DATASET":   os.environ.get("BQ_SILVER_DATASET", ""),
        "BQ_GOLD_DATASET":     os.environ.get("BQ_GOLD_DATASET", ""),
        "BQ_LOCATION":         os.environ.get("BQ_LOCATION", ""),
        "BQ_PUBLISHER_MODE":   os.environ.get("BQ_PUBLISHER_MODE", "auto"),
        "GCP_CREDENTIALS_PATH": os.environ.get("GCP_CREDENTIALS_PATH", ""),
        "AGENT_LOG_LEVEL":     os.environ.get("AGENT_LOG_LEVEL", ""),
    }
    for key, val in checks.items():
        status = "[OK]" if val and not val.startswith("your-") else "[X] NOT SET"
        display = val if val else "(not set)"
        # mask path-like values
        if "PATH" in key and val:
            display = f"...{val[-30:]}" if len(val) > 30 else val
        print(f"  {status}  {key:35s} = {display}")

    missing = _check_env()
    if missing:
        print(f"\n[!] Missing required variables: {missing}")
        print(f"  Copy .env.example to .env and fill in the values.")
    else:
        print("\n[OK] All required environment variables are set.")

    # Check ADC
    creds_path = os.environ.get("GCP_CREDENTIALS_PATH", "") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if creds_path and Path(creds_path).is_file():
        print(f"\n[OK] Service account key found at: {creds_path}")
    else:
        adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if adc_path.is_file():
            print(f"\n[OK] Application Default Credentials found at: {adc_path}")
        else:
            print(f"\n[!] No credentials found. Run: gcloud auth application-default login")


def cmd_server():
    """Start the FastAPI server with uvicorn."""
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"\n=== BFSI-Silver-Agent Server ===")
    print(f"  Starting on http://{host}:{port}")
    print(f"  Docs: http://localhost:{port}/docs")
    print(f"  GCP Project: {os.environ.get('GCP_PROJECT_ID', '(not set)')}")
    print(f"  Gemini Model: {os.environ.get('GEMINI_MODEL', '(not set)')}")
    print(f"  Publisher Mode: {os.environ.get('BQ_PUBLISHER_MODE', 'auto')}\n")

    missing = _check_env()
    if missing:
        print(f"WARNING: Missing env vars: {missing}")
        print("Set GCP_PROJECT_ID and GEMINI_MODEL in .env before making API calls.\n")

    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=os.environ.get("DEV_MODE", "false").lower() == "true",
        log_level=os.environ.get("AGENT_LOG_LEVEL", "info").lower(),
    )


def cmd_pipeline(args: list[str]):
    """Run the pipeline (interactive or from JSON file)."""
    from pipeline import _main as pipeline_main
    old_argv = sys.argv
    sys.argv = [sys.argv[0]] + args
    try:
        asyncio.run(pipeline_main())
    finally:
        sys.argv = old_argv


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] in ("--help", "-h", "help"):
        print(__doc__)
        sys.exit(0)

    command = args[0]
    rest = args[1:]

    if command == "server":
        cmd_server()
    elif command == "pipeline":
        cmd_pipeline(rest)
    elif command in ("validate-env", "validate"):
        cmd_validate_env()
    else:
        print(f"Unknown command: {command}")
        print("Commands: server, pipeline, validate-env")
        sys.exit(1)
