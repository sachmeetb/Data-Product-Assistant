"""Open Knowledge Format (OKF) export endpoints.

Hands an OKF bundle (markdown + YAML frontmatter, cross-linked by :CONSUMES) to
an external AI agent that can't reach our MCP/graph — a portable *briefing* that
complements the ODCS contract + dbt project we already export. Two scopes:

  GET /api/marketplace/okf-bundle                     → whole marketplace
  GET /api/marketplace/products/{contract_id}/okf-bundle → one product

Both accept ?format=zip (default) | json. Read-only; nothing is persisted.
"""

import io
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query as QParam, Response
from sqlmodel import Session

from ..database import get_session
from .. import okf_export
from .marketplace import _get_settings, _neo4j_from_settings

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])

_RESOLVE_URI = """\
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
RETURN dp.uri AS uri
"""


def _zip_response(files: dict[str, str], bundle_name: str) -> Response:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            zf.writestr(f"{bundle_name}/{rel}", content)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{bundle_name}-{ts}.zip"'},
    )


@router.get("/okf-bundle")
def get_marketplace_okf_bundle(
    format: str = QParam("zip", description="zip (default) streams a bundle; json returns {files: {...}}"),
    session: Session = Depends(get_session),
):
    """Export the whole marketplace as one cross-linked OKF bundle — catalog.md +
    products/<code>/{product,datasets/*,quality,reflection}.md, with :CONSUMES
    rendered as bundle-relative links so products built on each other are navigable."""
    settings = _get_settings(session)
    files = okf_export.collect_okf_files(settings)
    if not files:
        raise HTTPException(404, "No published products to export.")
    if format == "json":
        return {"file_count": len(files), "files": files}
    return _zip_response(files, "okf-marketplace")


@router.get("/products/{contract_id}/okf-bundle")
def get_product_okf_bundle(
    contract_id: str,
    format: str = QParam("zip", description="zip (default) streams a bundle; json returns {files: {...}}"),
    session: Session = Depends(get_session),
):
    """Export a single published product as a self-contained OKF bundle."""
    settings = _get_settings(session)
    with _neo4j_from_settings(settings) as ns:
        row = ns.run(_RESOLVE_URI, contract_id=contract_id).single()
    if not row or not row.get("uri"):
        raise HTTPException(404, f"No data product for contract '{contract_id}'.")
    files = okf_export.collect_okf_files(settings, uri=row["uri"])
    if not files:
        raise HTTPException(404, "Product has no exportable content.")
    if format == "json":
        return {"contract_id": contract_id, "file_count": len(files), "files": files}
    return _zip_response(files, f"okf-{okf_export._slug(contract_id)}")
