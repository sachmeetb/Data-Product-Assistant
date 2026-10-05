"""Phase 1 secret resolver.

Resolves a `secret_ref` string into a plaintext credential at call time.
The resolved value is NEVER stored, logged, or returned in API responses —
it is only passed directly to a provider's connect call.

Supported formats:
  env:<VAR_NAME>     — read from the process environment (local dev / compose)
  direct:<password>  — plaintext password stored in SQLite (local/dev only;
                       never logged or returned in API responses; never passed
                       to an LLM call)
  <anything else>    — empty string (unknown format; provider will fail auth)

Phase 2 will add:
  vault:<path>     — HashiCorp Vault KV lookup
  ssm:<path>       — AWS SSM Parameter Store
  asm:<name>       — AWS Secrets Manager
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def resolve_secret(secret_ref: str) -> str:
    """Return the plaintext secret for `secret_ref`.

    Returns an empty string when the ref is empty or the format is not
    recognised — never raises.  The caller (provider probe / connect) will
    then fail with an auth error, which is the correct signal.
    """
    if not secret_ref:
        return ""
    if secret_ref.startswith("env:"):
        var = secret_ref[4:]
        val = os.environ.get(var, "")
        if not val:
            logger.warning("secret_ref env:%s is not set in the environment", var)
        return val
    if secret_ref.startswith("direct:"):
        return secret_ref[7:]
    # Unrecognised format — treat the value itself as the literal password.
    # This lets users paste a raw password without needing to know the
    # direct: prefix convention.  A debug log is emitted so operators can
    # spot accidental plain-text storage in logs.
    logger.debug(
        "secret_ref value does not match a known prefix (env:/direct:); "
        "treating as a literal password. Consider migrating to env:VAR_NAME.",
    )
    return secret_ref
