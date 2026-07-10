"""Multi-tenant credential routing — the SaaS SSOT seam.

One shim serves EVERY tenant. It holds no backend credentials of its own: on each governed call it
resolves the calling workspace's Odoo credentials from the control-plane vault (the single source of
truth) and drives that tenant's backend. `/describe` needs no credentials (a static verb catalog), so
the connect probe succeeds the instant the shim is up — before any tenant is provisioned. This is what
breaks the env-at-boot chicken-and-egg: the Connect page saves creds to the vault, and this resolver
reads them per request.

The current workspace for a request is carried in a ContextVar set by the edge (propose/commit/status)
from the NIL envelope's `workspace`. A per-tenant `RealSystemClient` is built once and cached.
"""

from __future__ import annotations

import contextvars
import os
from typing import Any

import httpx

from odoo_nil_adapter.system import RealSystemClient, SystemClient, SystemError

# Set by the edge at the top of each governed request from env["workspace"]; None = no tenant scope
# (e.g. the shipped skeleton / describe), which routes to no backend and fails closed on a real op.
_current_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "odoo_current_tenant", default=None
)


def bind_tenant(workspace: str | None) -> None:
    _current_tenant.set(workspace or None)


class VaultResolvingClient:
    """A `SystemClient` whose real backend is chosen per request by the current-tenant ContextVar.

    Each method resolves (and caches) that tenant's `RealSystemClient` from the vault, then delegates.
    A request with no tenant, or a tenant with no provisioned Odoo secrets, fails closed with a clear
    SystemError — never silently touches another tenant's backend."""

    # The vault secret names this adapter provisions/reads for a tenant's Odoo backend.
    _SECRET_KEYS = ("odoo_url", "odoo_db", "odoo_login", "odoo_api_key")

    def __init__(self, registry_url: str, registry_token: str) -> None:
        self._registry_url = registry_url.rstrip("/")
        self._registry_token = registry_token
        self._cache: dict[str, RealSystemClient] = {}

    def _fetch_secret(self, workspace: str, name: str) -> str | None:
        url = f"{self._registry_url}/tenants/{workspace}/secret/{name}"
        headers = {"Authorization": f"Bearer {self._registry_token}"} if self._registry_token else {}
        try:
            r = httpx.get(url, headers=headers, timeout=8)
        except httpx.HTTPError as exc:
            raise SystemError(f"vault unreachable while resolving {name} for {workspace!r}: {exc}") from exc
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise SystemError(f"vault denied {name} for {workspace!r} (HTTP {r.status_code})")
        return (r.json() or {}).get("value")

    def _real(self) -> RealSystemClient:
        ws = _current_tenant.get()
        if not ws:
            raise SystemError("no workspace in the request — this multi-tenant shim cannot route a "
                              "credential-less call to any backend")
        cached = self._cache.get(ws)
        if cached is not None:
            return cached
        secrets = {k: self._fetch_secret(ws, k) for k in self._SECRET_KEYS}
        missing = [k for k in ("odoo_url", "odoo_db", "odoo_login", "odoo_api_key") if not secrets.get(k)]
        if missing:
            raise SystemError(
                f"workspace {ws!r} has no Odoo credentials provisioned (missing {', '.join(missing)}) — "
                "connect Odoo CRM on the Systems page first"
            )
        client = RealSystemClient(
            secrets["odoo_url"], db=secrets["odoo_db"],
            login=secrets["odoo_login"], api_key=secrets["odoo_api_key"],
        )
        self._cache[ws] = client
        return client

    def invalidate(self, workspace: str) -> None:
        """Drop a tenant's cached client (call after a credential rotation)."""
        self._cache.pop(workspace, None)

    # ── SystemClient delegation (every method routes to the current tenant's real client) ──
    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        return self._real().create(target, doc)

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._real().list(target, filters)

    def count(self, target: str, domain: list[list[Any]]) -> int:
        return self._real().count(target, domain)

    def search(
        self,
        target: str,
        domain: list[list[Any]],
        *,
        fields: tuple[str, ...] | None = None,
        limit: int = 50,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._real().search(target, domain, fields=fields, limit=limit, order=order)

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        return self._real().update(target, record_id, doc)

    def message_post(self, target: str, record_id: str, body: str) -> None:
        return self._real().message_post(target, record_id, body)

    def call_method(self, target: str, record_id: str, method: str, params: dict[str, Any]) -> Any:
        return self._real().call_method(target, record_id, method, params)

    def delete(self, target: str, record_id: str) -> None:
        return self._real().delete(target, record_id)

    def exists(self, target: str) -> bool:
        return self._real().exists(target)

    def schema(self, target: str) -> list[dict[str, Any]] | None:
        return self._real().schema(target)

    def get(self, target: str, record_id: str) -> dict[str, Any] | None:
        return self._real().get(target, record_id)


def build_from_env() -> SystemClient | None:
    """A VaultResolvingClient when NIL_REGISTRY_URL is set (SaaS multi-tenant mode), else None
    (the caller falls back to the single-tenant env-cred client)."""
    registry_url = os.environ.get("NIL_REGISTRY_URL", "").strip()
    if not registry_url:
        return None
    return VaultResolvingClient(registry_url, os.environ.get("NIL_REGISTRY_TOKEN", ""))
