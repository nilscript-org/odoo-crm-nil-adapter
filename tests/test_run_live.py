"""U5 round 3: `run_live.build_app` fails loud on a malformed `NIL_SERVICE_KEY` — never silently
falls back to the shared secret/token. Single-tenant (`ODOO_*` creds, no `NIL_REGISTRY_URL`) so the
ONLY service-key parse on this path is the one `build_app` does for the event emitter's `own_key` —
isolating that call from `tenant_routing.build_from_env`'s own (already-covered) parse for the vault
bearer. `RealSystemClient`'s constructor does no I/O, so dummy creds are safe here."""

from __future__ import annotations

import pytest

from odoo_nil_adapter.run_live import build_app

_ODOO_ENV = {
    "ODOO_URL": "https://odoo.example",
    "ODOO_DB": "db",
    "ODOO_LOGIN": "user@example.com",
    "ODOO_API_KEY": "key",
}


def _set_odoo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _ODOO_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("NIL_REGISTRY_URL", raising=False)


def test_build_app_raises_on_malformed_service_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_odoo_env(monkeypatch)
    monkeypatch.setenv("NIL_EVENTS_WEBHOOK", "https://cp.example/events")
    monkeypatch.setenv("NIL_SERVICE_KEY", "nocolon")

    with pytest.raises(ValueError, match="NIL_SERVICE_KEY"):
        build_app()


def test_build_app_boots_with_well_formed_service_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_odoo_env(monkeypatch)
    monkeypatch.setenv("NIL_EVENTS_WEBHOOK", "https://cp.example/events")
    monkeypatch.setenv("NIL_SERVICE_KEY", "kid-example:0123456789abcdef0123456789abcdef")

    build_app()  # must not raise
