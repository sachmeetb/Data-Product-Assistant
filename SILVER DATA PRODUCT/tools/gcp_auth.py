"""
gcp_auth.py — GCP Authentication helper for BFSI-Silver-Agent.

Supports two authentication modes:
  1. Service Account JSON key file  (explicit credentials via GCP_CREDENTIALS_PATH)
  2. Application Default Credentials (ADC) — gcloud auth / Workload Identity

Priority order:
  1. credentials_path argument (explicit override)
  2. GCP_CREDENTIALS_PATH env var
  3. GOOGLE_APPLICATION_CREDENTIALS env var
  4. ADC (gcloud auth application-default login or GCE/GKE metadata server)
"""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Configure SSL context for corporate inspection proxies
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass

log = logging.getLogger(__name__)


def get_credentials(
    credentials_path: Optional[str] = None,
    scopes: Optional[list[str]] = None,
):
    """
    Resolve GCP credentials.

    Returns google.oauth2.service_account.Credentials if a key file is found,
    or None to trigger ADC in the calling client library.
    """
    scopes = scopes or ["https://www.googleapis.com/auth/cloud-platform"]

    path = (
        credentials_path
        or os.environ.get("GCP_CREDENTIALS_PATH", "")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    )

    if path and Path(path).is_file():
        try:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                path, scopes=scopes
            )
            log.info("GCP auth: service account key from %s", path)
            return creds
        except Exception as exc:
            log.warning("Failed to load service account key %s: %s", path, exc)

    log.info("GCP auth: using Application Default Credentials (ADC)")
    return None  # client libraries use ADC automatically when credentials=None


def get_bigquery_client(
    project_id: Optional[str] = None,
    credentials_path: Optional[str] = None,
    location: Optional[str] = None,
):
    """
    Build and return a google.cloud.bigquery.Client.

    Args:
        project_id:       GCP project. Falls back to GCP_PROJECT_ID env var.
        credentials_path: SA JSON key path. Falls back to env vars / ADC.
        location:         Default BQ query location. Falls back to BQ_LOCATION or 'US'.
    """
    from google.cloud import bigquery

    project = project_id or os.environ.get("GCP_PROJECT_ID", "")
    loc = location or os.environ.get("BQ_LOCATION", "US")
    creds = get_credentials(credentials_path)

    client = bigquery.Client(
        project=project,
        credentials=creds,  # None → ADC
        location=loc,
    )
    log.info("BigQuery client ready: project=%s location=%s", project, loc)
    return client


def get_vertexai_credentials(
    credentials_path: Optional[str] = None,
    project_id: Optional[str] = None,
    location: Optional[str] = None,
) -> None:
    """
    Initialise Vertex AI SDK with resolved credentials.
    Call once at application startup before creating any Gemini agents.

    Args:
        credentials_path: SA JSON key path. Falls back to env vars / ADC.
        project_id:       GCP project. Falls back to GCP_PROJECT_ID env var.
        location:         Vertex AI region. Falls back to GCP_LOCATION or 'us-central1'.

    Raises:
        EnvironmentError: if GCP_PROJECT_ID is not set.
    """
    import vertexai

    project = project_id or os.environ.get("GCP_PROJECT_ID", "")
    loc = location or os.environ.get("GCP_LOCATION", "us-central1")

    if not project:
        raise EnvironmentError(
            "GCP_PROJECT_ID is not set. "
            "Add it to BFSI-Silver-Agent/.env or set the environment variable."
        )

    creds = get_credentials(credentials_path)

    if creds:
        vertexai.init(project=project, location=loc, credentials=creds)
    else:
        vertexai.init(project=project, location=loc)

    log.info("Vertex AI initialised: project=%s location=%s", project, loc)
