"""
session_store.py — Redis-backed, dict-compatible session store.

Replaces the in-memory `_sessions` dict in server.py so conversational state
survives process restarts and is shared across all gunicorn workers (startup.sh
runs `gunicorn -w 4`, where each worker otherwise has its own private dict — a
request routed to a different worker would not find the session and the
orchestrator would fall back to the welcome menu mid-flow).

It implements only the dict operations server.py uses — `get`, `[]=`, `in`,
`setdefault` — so it is a drop-in replacement for the plain dict.

ADK session IDs are plain strings and serialize to Redis without special handling.
The in-process _local dict is kept for any future non-serializable fragments but
is currently unused.

If Redis cannot be reached, the store degrades gracefully to an in-process
dict (same behaviour as before this change) and logs a warning, so local
development without a Redis server still works.
"""

import json
import os
from typing import Any

_MISSING = object()

# Session-dict keys whose values are live objects that cannot be JSON-encoded.
_NON_SERIALIZABLE_KEYS = ()


class SessionStore:
    """Dict-compatible session store backed by Redis (with in-memory fallback)."""

    def __init__(
        self,
        prefix: str = "session:",
        ttl_seconds: int = 86_400,
        redis_url: str | None = None,
    ):
        self._prefix = prefix
        self._ttl = ttl_seconds
        self._local: dict[str, dict] = {}   # per-process non-serializable fragments
        self._mem: dict[str, dict] = {}     # fallback store used when Redis is down
        self._redis = None

        url = redis_url or os.environ.get("REDIS_URL")
        if not url:
            # Redis not configured (e.g. not yet provisioned on Azure). Use
            # in-process memory — identical to the behaviour before Redis was
            # introduced. Set REDIS_URL later to enable shared/persistent
            # sessions across workers and restarts; no code change needed.
            print(
                f"[session-store] REDIS_URL not set — using in-process memory "
                f"(prefix={prefix!r})."
            )
            self._redis = None
            return
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(
                url, decode_responses=True, socket_connect_timeout=2
            )
            client.ping()
            self._redis = client
            print(f"[session-store] Using Redis at {url} (prefix={prefix!r}).")
        except Exception as exc:  # ImportError, connection error, auth, etc.
            print(
                f"[session-store] Redis unavailable ({exc!r}); falling back to "
                f"in-process memory (prefix={prefix!r}). Sessions will NOT survive "
                f"restarts or be shared across workers."
            )
            self._redis = None

    # ── internal: split/merge the serializable vs. live parts ────────────────

    def _split(self, value: dict) -> tuple[dict, dict]:
        store, local = {}, {}
        for k, v in value.items():
            (local if k in _NON_SERIALIZABLE_KEYS else store)[k] = v
        return store, local

    def _read_store(self, key: str):
        """Return the JSON-serializable fragment for key, or None if absent."""
        if self._redis is not None:
            try:
                raw = self._redis.get(self._prefix + key)
                return json.loads(raw) if raw else None
            except Exception as exc:
                print(f"[session-store] Redis GET failed ({exc!r}); using memory.")
        return self._mem.get(key)

    # ── dict-compatible API (only what server.py uses) ───────────────────────

    def __setitem__(self, key: str, value: dict) -> None:
        if not isinstance(value, dict):
            raise TypeError("SessionStore values must be dicts.")
        store, local = self._split(value)
        # Keep any non-serializable fragment in this process only.
        if local:
            self._local[key] = {**self._local.get(key, {}), **local}

        wrote = False
        if self._redis is not None:
            try:
                self._redis.set(
                    self._prefix + key,
                    json.dumps(store, default=str),
                    ex=self._ttl,
                )
                wrote = True
            except Exception as exc:
                print(f"[session-store] Redis SET failed ({exc!r}); using memory.")
        if not wrote:
            self._mem[key] = store

    def get(self, key: str, default: Any = None):
        store = self._read_store(key)
        if store is None:
            return default
        local = self._local.get(key, {})
        return {**store, **local} if local else dict(store)

    def __getitem__(self, key: str):
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __contains__(self, key: str) -> bool:
        return self._read_store(key) is not None

    def setdefault(self, key: str, default: dict):
        value = self.get(key, _MISSING)
        if value is _MISSING:
            self[key] = default
            return default
        return value
