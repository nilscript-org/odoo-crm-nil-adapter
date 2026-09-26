"""Control-plane (vault) auth headers — this adapter's own service key, shared bearer as fallback.

The control plane now dual-accepts, for one release, either a per-service key or the old shared
bearer token. A service holds `NIL_SERVICE_KEY=<kid>:<secret>` and signs a fixed header:

    Authorization: Bearer nilk1.<kid>.<lowercase hex HMAC_SHA256(key=secret, msg=b"nil.bearer.v1\\n" + kid)>

This adapter installs only the OPEN kernel (no `services.security.service_keys` to import), so the
2-line HMAC is reimplemented here with stdlib `hmac`/`hashlib`, pinned to the hub's literal vector:
kid "kid-example", secret "0123456789abcdef0123456789abcdef" →
"Bearer nilk1.kid-example.897ce1af1bd86c9c4c7560f41de7e54eb23c29d7a9d3ed6df4af8913a7e9d262".

Precedence: `NIL_SERVICE_KEY` (non-blank) wins over `NIL_REGISTRY_TOKEN`; a malformed
`NIL_SERVICE_KEY` raises rather than silently falling back to the shared token — a typo'd key must
fail loud, never quietly downgrade to a weaker credential.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Mapping

_SIG_PREFIX = b"nil.bearer.v1\n"


def bearer(kid: str, secret: str) -> str:
    """The full `Authorization` header value for a per-service key."""
    mac = hmac.new(secret.encode("utf-8"), _SIG_PREFIX + kid.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"Bearer nilk1.{kid}.{mac}"


def _parse_service_key(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        raise ValueError("NIL_SERVICE_KEY must be '<kid>:<secret>'")
    kid, secret = raw.split(":", 1)
    kid = kid.strip()
    secret = secret.strip()
    if not kid or not secret:
        raise ValueError("NIL_SERVICE_KEY must be '<kid>:<secret>' with both parts non-empty")
    return kid, secret


def auth_headers(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`{"Authorization": ...}` (or `{}`) from the environment: own service key first, then the
    shared registry token, else no header at all."""
    import os

    env = environ if environ is not None else os.environ
    service_key = (env.get("NIL_SERVICE_KEY") or "").strip()
    if service_key:
        kid, secret = _parse_service_key(service_key)
        return {"Authorization": bearer(kid, secret)}
    registry_token = (env.get("NIL_REGISTRY_TOKEN") or "").strip()
    if registry_token:
        return {"Authorization": f"Bearer {registry_token}"}
    return {}
