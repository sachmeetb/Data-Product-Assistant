"""
session_store.py — In-memory session store with optional Redis backend.

Used by the FastAPI server to persist pipeline state across requests.

Backends:
  InMemorySessionStore  — default, dev-only (process-local)
  RedisSessionStore     — production multi-replica deployment

Controlled via SESSION_BACKEND env var: "memory" (default) or "redis".
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


class InMemorySessionStore:
    """Thread-safe in-memory store. Suitable for single-process dev/test."""

    def __init__(self):
        self._store: dict[str, dict] = {}

    def get(self, session_id: str) -> Optional[dict]:
        return self._store.get(session_id)

    def set(self, session_id: str, data: dict) -> None:
        self._store[session_id] = data

    def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)

    def list_sessions(self) -> list[str]:
        return list(self._store.keys())


class RedisSessionStore:
    """Redis-backed session store for multi-replica production deployment."""

    def __init__(self, url: Optional[str] = None, ttl_seconds: int = 86400):
        import redis
        redis_url = url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self._client = redis.Redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_seconds
        log.info("RedisSessionStore connected to %s", redis_url)

    def get(self, session_id: str) -> Optional[dict]:
        raw = self._client.get(f"bfsi:session:{session_id}")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def set(self, session_id: str, data: dict) -> None:
        self._client.setex(
            f"bfsi:session:{session_id}",
            self._ttl,
            json.dumps(data, default=str),
        )

    def delete(self, session_id: str) -> None:
        self._client.delete(f"bfsi:session:{session_id}")

    def list_sessions(self) -> list[str]:
        keys = self._client.keys("bfsi:session:*")
        return [k.removeprefix("bfsi:session:") for k in keys]


def SessionStore():
    """
    Factory function — returns the appropriate session store based on
    SESSION_BACKEND env var ("memory" or "redis").
    """
    backend = os.environ.get("SESSION_BACKEND", "memory").strip().lower()

    if backend == "redis":
        try:
            store = RedisSessionStore()
            log.info("Using RedisSessionStore")
            return store
        except Exception as exc:
            log.warning("Redis init failed (%s) — falling back to InMemory", exc)

    log.info("Using InMemorySessionStore")
    return InMemorySessionStore()
