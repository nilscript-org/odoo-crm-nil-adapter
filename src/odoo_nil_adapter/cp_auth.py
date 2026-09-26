"""Control-plane auth: this adapter's own service key, a shared secret as fallback.

The control plane now dual-accepts, for one release, either a per-service key or the old shared
credential — on the vault bearer AND on posted EVENTs. A service holds `NIL_SERVICE_KEY=<kid>:<secret>`
(parsed once here, reused by both signers below).

Vault bearer:

    Authorization: Bearer nilk1.<kid>.<lowercase hex HMAC_SHA256(key=secret, msg=b"nil.bearer.v1\\n" + kid)>

pinned to the hub's literal vector: kid "kid-example", secret "0123456789abcdef0123456789abcdef" →
"Bearer nilk1.kid-example.897ce1af1bd86c9c4c7560f41de7e54eb23c29d7a9d3ed6df4af8913a7e9d262".

Event signature (posted alongside `X-NIL-Key-Id: <kid>` in place of the shared-secret signature):

    X-NIL-Signature: <lowercase hex HMAC_SHA256(key=secret, msg=b"nil.event.v1\\n" + raw_body_bytes)>

pinned to the hub's literal EVENT_TEST_VECTOR: secret "fedcba9876543210fedcba9876543210",
raw `b'{"type":"test.vector","n":1}'` →
"d83b9e63c855e9c72b44c8b829627599a629204bd1b33f7336e3f792e92d9b15".

This adapter installs only the OPEN kernel (no `services.security.service_keys` to import), so both
2-line HMACs are reimplemented here with stdlib `hmac`/`hashlib`.

Precedence: `NIL_SERVICE_KEY` (non-blank) wins over the shared credential (`NIL_REGISTRY_TOKEN` for
the vault bearer, `NIL_EVENTS_SECRET` for events); a malformed `NIL_SERVICE_KEY` raises rather than
silently falling back to the shared credential — a typo'd key must fail loud, never quietly downgrade.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Mapping

_BEARER_SIG_PREFIX = b"nil.bearer.v1\n"
_EVENT_SIG_PREFIX = b"nil.event.v1\n"


def bearer(kid: str, secret: str) -> str:
    """The full `Authorization` header value for a per-service key."""
    mac = hmac.new(
        secret.encode("utf-8"), _BEARER_SIG_PREFIX + kid.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"Bearer nilk1.{kid}.{mac}"


def event_mac(secret: str, raw: bytes) -> str:
    """Lowercase hex HMAC-SHA256 of an event's raw posted body, under a per-service key's secret."""
    return hmac.new(secret.encode("utf-8"), _EVENT_SIG_PREFIX + raw, hashlib.sha256).hexdigest()


def parse_service_key(raw: str) -> tuple[str, str]:
    """`"<kid>:<secret>"` → `(kid, secret)`. Split on the FIRST `:`; both parts stripped and
    required non-empty. Raises `ValueError` naming `NIL_SERVICE_KEY` on any other shape — a
    malformed key must never silently fall back to a shared credential."""
    if ":" not in raw:
        raise ValueError("NIL_SERVICE_KEY must be '<kid>:<secret>'")
    kid, secret = raw.split(":", 1)
    kid = kid.strip()
    secret = secret.strip()
    if not kid or not secret:
        raise ValueError("NIL_SERVICE_KEY must be '<kid>:<secret>' with both parts non-empty")
    return kid, secret


def service_key_from_env(environ: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """The parsed `(kid, secret)` from `NIL_SERVICE_KEY` if set (non-blank), else `None`. A
    malformed non-blank value raises — the one parse reused by both the bearer and event signers."""
    import os

    env = environ if environ is not None else os.environ
    service_key = (env.get("NIL_SERVICE_KEY") or "").strip()
    if not service_key:
        return None
    return parse_service_key(service_key)


def auth_headers(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`{"Authorization": ...}` (or `{}`) from the environment: own service key first, then the
    shared registry token, else no header at all."""
    parsed = service_key_from_env(environ)
    if parsed is not None:
        kid, secret = parsed
        return {"Authorization": bearer(kid, secret)}
    import os

    env = environ if environ is not None else os.environ
    registry_token = (env.get("NIL_REGISTRY_TOKEN") or "").strip()
    if registry_token:
        return {"Authorization": f"Bearer {registry_token}"}
    return {}
