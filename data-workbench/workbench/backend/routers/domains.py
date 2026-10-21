from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..database import get_session
from ..models import AppSettings
from ..neo4j_client import neo4j_session

router = APIRouter(prefix="/api/domains", tags=["domains"])

QUERY_DOMAINS = """\
MATCH (p:Project)
WHERE p.domain IS NOT NULL AND trim(p.domain) <> ''
RETURN DISTINCT p.domain AS name
ORDER BY name
"""

ADD_DOMAIN = """\
MERGE (d:Domain {name: $name})
ON CREATE SET d.createdAt = datetime()
RETURN d.name AS name
"""


class DomainCreate(BaseModel):
    name: str


def _get_neo4j_settings(session: Session):
    settings = session.exec(select(AppSettings)).first()
    if not settings:
        raise HTTPException(500, "Global Neo4j settings not configured. Go to Settings first.")
    return settings


@router.get("")
def list_domains(session: Session = Depends(get_session)):
    """Return the distinct ``:Project.domain`` slugs from the graph.

    Previously sourced from a parallel ``:Domain`` catalog seeded with
    label-style names ("Customer", "Sales & Marketing"), which drifted
    from the slug-form values (``"products_sales"``) used everywhere
    downstream (marketplace, chat, scoring). Concepts accepted against
    a catalog-style domain became invisible to the chat. Single source
    of truth eliminates that class of bug.
    """
    settings = _get_neo4j_settings(session)
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        domains = [r["name"] for r in ns.run(QUERY_DOMAINS)]
    return {"domains": domains}


@router.post("")
def create_domain(body: DomainCreate, session: Session = Depends(get_session)):
    """Add a custom domain to Neo4j."""
    settings = _get_neo4j_settings(session)

    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        result = ns.run(ADD_DOMAIN, name=body.name.strip())
        record = result.single()
        if not record:
            raise HTTPException(500, "Failed to create domain")

    return {"name": record["name"]}
