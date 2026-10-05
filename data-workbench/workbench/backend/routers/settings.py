from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from ..database import get_session
from ..models import AppSettings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    neo4j_host: Optional[str] = None
    neo4j_port: Optional[int] = None
    neo4j_user: Optional[str] = None
    neo4j_password: Optional[str] = None
    neo4j_database: Optional[str] = None
    neo4j_browser_url: Optional[str] = None
    git_provider: Optional[str] = None
    git_base_url: Optional[str] = None
    git_web_base_url: Optional[str] = None
    git_token: Optional[str] = None
    git_org: Optional[str] = None
    git_auto_push: Optional[bool] = None


def _settings_dict(settings: AppSettings) -> dict:
    return {
        "neo4j_host": settings.neo4j_host,
        "neo4j_port": settings.neo4j_port,
        "neo4j_user": settings.neo4j_user,
        "neo4j_password": settings.neo4j_password,
        "neo4j_database": settings.neo4j_database,
        "neo4j_browser_url": settings.neo4j_browser_url,
        "git_provider": getattr(settings, "git_provider", None),
        "git_base_url": getattr(settings, "git_base_url", None),
        "git_web_base_url": getattr(settings, "git_web_base_url", None),
        # Never leak the token to the client — only whether one is set.
        "git_token_set": bool(getattr(settings, "git_token", None)),
        "git_org": getattr(settings, "git_org", None),
        "git_auto_push": bool(getattr(settings, "git_auto_push", False)),
    }


def _get_or_create(session: Session) -> AppSettings:
    settings = session.exec(select(AppSettings)).first()
    if not settings:
        settings = AppSettings()
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings


@router.get("")
def get_settings(session: Session = Depends(get_session)):
    return _settings_dict(_get_or_create(session))


@router.put("")
def update_settings(body: SettingsUpdate, session: Session = Depends(get_session)):
    settings = _get_or_create(session)
    for key, val in body.model_dump(exclude_none=True).items():
        setattr(settings, key, val)
    session.add(settings)
    session.commit()
    session.refresh(settings)
    return _settings_dict(settings)


@router.get("/git/test")
def test_git_connection(session: Session = Depends(get_session)):
    """Cheap auth probe against the configured git provider (Gitea /user or
    GitHub /user). Returns ``{ok, provider, detail|error}``."""
    from ..git_provider import get_provider, GitProviderError

    settings = _get_or_create(session)
    try:
        provider = get_provider(settings)
    except GitProviderError as exc:
        return {"ok": False, "error": str(exc)}
    if provider is None:
        return {"ok": False, "error": "Git provider not configured"}
    result = provider.test_connection()
    result["provider"] = settings.git_provider
    return result
