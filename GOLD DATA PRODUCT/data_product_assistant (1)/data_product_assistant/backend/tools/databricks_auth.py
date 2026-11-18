"""
databricks_auth.py — Azure Databricks authentication & client factory for Gold Data Product Assistant.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


def get_workspace_client(
    host: Optional[str] = None,
    token: Optional[str] = None,
):
    """Build and return a databricks.sdk.WorkspaceClient."""
    from databricks.sdk import WorkspaceClient

    resolved_host = host or os.environ.get("DATABRICKS_HOST", "")
    resolved_token = token or os.environ.get("DATABRICKS_TOKEN", "")
    kwargs: dict = {}
    if resolved_host:
        kwargs["host"] = resolved_host
    if resolved_token:
        kwargs["token"] = resolved_token
    return WorkspaceClient(**kwargs)


def get_sql_connection(
    host: Optional[str] = None,
    http_path: Optional[str] = None,
    token: Optional[str] = None,
    catalog: Optional[str] = None,
    schema: Optional[str] = None,
):
    """Build and return a databricks.sql.Connection for a Databricks SQL Warehouse."""
    from databricks import sql as dbsql

    raw_host = (host or os.environ.get("DATABRICKS_HOST", "")).strip()
    server_hostname = raw_host.removeprefix("https://").removeprefix("http://").rstrip("/")
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
    resolved_http_path = (
        http_path
        or os.environ.get("DATABRICKS_HTTP_PATH", "").strip()
        or (f"/sql/1.0/warehouses/{warehouse_id}" if warehouse_id else "")
    )
    resolved_token = (token or os.environ.get("DATABRICKS_TOKEN", "")).strip()

    if not server_hostname or not resolved_http_path:
        raise EnvironmentError(
            "DATABRICKS_HOST and DATABRICKS_WAREHOUSE_ID (or DATABRICKS_HTTP_PATH) must be set."
        )

    connect_kwargs: dict = {
        "server_hostname": server_hostname,
        "http_path": resolved_http_path,
    }
    if resolved_token:
        connect_kwargs["access_token"] = resolved_token
    else:
        wc = get_workspace_client(host=raw_host)
        connect_kwargs["credentials_provider"] = lambda: wc.config.authenticate

    if catalog:
        connect_kwargs["catalog"] = catalog
    if schema:
        connect_kwargs["schema"] = schema

    return dbsql.connect(**connect_kwargs)


def get_async_llm_client():
    """
    Return an AsyncOpenAI-compatible client targeting either Azure OpenAI
    (when AZURE_OPENAI_ENDPOINT is set) or Databricks Foundation Model APIs.
    """
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    if azure_endpoint:
        from openai import AsyncAzureOpenAI

        api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
        api_key = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
        if api_key:
            return AsyncAzureOpenAI(
                azure_endpoint=azure_endpoint,
                api_key=api_key,
                api_version=api_version,
                timeout=1800.0,
            )
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        return AsyncAzureOpenAI(
            azure_endpoint=azure_endpoint,
            azure_ad_token_provider=token_provider,
            api_version=api_version,
            timeout=1800.0,
        )

    from openai import AsyncOpenAI

    raw_host = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
    if not raw_host:
        raise EnvironmentError("DATABRICKS_HOST (or AZURE_OPENAI_ENDPOINT) must be set.")
    if not raw_host.startswith("http"):
        raw_host = f"https://{raw_host}"

    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if not token:
        wc = get_workspace_client(host=raw_host)
        headers = wc.config.authenticate()
        token = headers.get("Authorization", "").removeprefix("Bearer ").strip()

    return AsyncOpenAI(
        api_key=token,
        base_url=f"{raw_host}/serving-endpoints",
        timeout=1800.0,
    )
