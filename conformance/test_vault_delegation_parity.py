"""The vault client MUST delegate every public SystemClient method WITH the same signature —
a missing delegation (e.g. `search`, which 500'd every production read) or a dropped kwarg
(e.g. `order`, which 500'd every vault-mode projected read) must fail THIS test, not production."""

from __future__ import annotations

import inspect

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


def test_vault_delegations_accept_every_real_parameter():
    """Signature parity: each delegation must accept every parameter the real method accepts
    (same names, same kinds) — presence alone let `search(order=...)` slip through to a 500."""
    shared = _public_methods(RealSystemClient) & _public_methods(VaultResolvingClient)
    mismatches: list[str] = []
    for name in sorted(shared):
        real_params = inspect.signature(getattr(RealSystemClient, name)).parameters
        vault_params = inspect.signature(getattr(VaultResolvingClient, name)).parameters
        for pname, param in real_params.items():
            got = vault_params.get(pname)
            if got is None:
                mismatches.append(f"{name}: missing parameter {pname!r}")
            elif got.kind is not param.kind:
                mismatches.append(f"{name}: parameter {pname!r} kind {got.kind} != {param.kind}")
    assert not mismatches, "vault delegation signature drift:\n" + "\n".join(mismatches)
