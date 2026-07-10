"""The vault client MUST delegate every public SystemClient method — a missing delegation
(e.g. `search`, which 500'd every production read) must fail THIS test, not production."""

from __future__ import annotations

from odoo_nil_adapter.system import RealSystemClient
from odoo_nil_adapter.tenant_routing import VaultResolvingClient


def _public_methods(cls) -> set[str]:
    return {
        n for n, v in vars(cls).items()
        if callable(v) and not n.startswith("_")
    }


def test_vault_client_delegates_every_public_system_method():
    real = _public_methods(RealSystemClient)
    vault = _public_methods(VaultResolvingClient) - {"invalidate"}  # vault-only management API
    missing = real - vault
    assert not missing, f"VaultResolvingClient is missing delegations: {sorted(missing)}"
