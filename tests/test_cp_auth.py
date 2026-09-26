"""U5: the vault client sends its own service key (NIL_SERVICE_KEY), shared bearer as fallback.

Pins the hub's literal HMAC vector, the header-precedence order, fail-loud parsing of a malformed
key, and that `VaultResolvingClient._fetch_secret` actually sends the resolved header on the wire.
"""

from __future__ import annotations

from typing import Any

import pytest

from odoo_nil_adapter import cp_auth
from odoo_nil_adapter.tenant_routing import VaultResolvingClient

VECTOR_KID = "kid-example"
VECTOR_SECRET = "0123456789abcdef0123456789abcdef"
VECTOR_HEADER = (
    "Bearer nilk1.kid-example."
    "897ce1af1bd86c9c4c7560f41de7e54eb23c29d7a9d3ed6df4af8913a7e9d262"
)

EVENT_TEST_SECRET = "fedcba9876543210fedcba9876543210"
EVENT_TEST_RAW = b'{"type":"test.vector","n":1}'
EVENT_TEST_MAC = "d83b9e63c855e9c72b44c8b829627599a629204bd1b33f7336e3f792e92d9b15"


def test_bearer_matches_hub_vector_byte_for_byte() -> None:
    assert cp_auth.bearer(VECTOR_KID, VECTOR_SECRET) == VECTOR_HEADER


def test_event_mac_matches_hub_vector_byte_for_byte() -> None:
    assert cp_auth.event_mac(EVENT_TEST_SECRET, EVENT_TEST_RAW) == EVENT_TEST_MAC


def test_service_key_from_env_none_when_unset() -> None:
    assert cp_auth.service_key_from_env({}) is None


def test_service_key_from_env_parses_when_set() -> None:
    assert cp_auth.service_key_from_env(
        {"NIL_SERVICE_KEY": f"{VECTOR_KID}:{VECTOR_SECRET}"}
    ) == (VECTOR_KID, VECTOR_SECRET)


@pytest.mark.parametrize("raw", ["nocolon", ":x", "k:"])
def test_service_key_from_env_raises_on_malformed(raw: str) -> None:
    with pytest.raises(ValueError, match="NIL_SERVICE_KEY"):
        cp_auth.service_key_from_env({"NIL_SERVICE_KEY": raw})


def test_auth_headers_prefers_service_key() -> None:
    env = {"NIL_SERVICE_KEY": f"{VECTOR_KID}:{VECTOR_SECRET}", "NIL_REGISTRY_TOKEN": "tok"}
    assert cp_auth.auth_headers(env) == {"Authorization": VECTOR_HEADER}


def test_auth_headers_falls_back_to_registry_token() -> None:
    assert cp_auth.auth_headers({"NIL_REGISTRY_TOKEN": "tok"}) == {"Authorization": "Bearer tok"}


def test_auth_headers_no_credentials_means_no_header() -> None:
    assert cp_auth.auth_headers({}) == {}


@pytest.mark.parametrize("raw", ["nocolon", ":x", "k:", "  :  ", ""])
def test_malformed_service_key_raises_and_does_not_fall_back(raw: str) -> None:
    env = {"NIL_SERVICE_KEY": raw, "NIL_REGISTRY_TOKEN": "tok"}
    if raw == "":
        # blank NIL_SERVICE_KEY is "not set" — falls back to the registry token, not an error.
        assert cp_auth.auth_headers(env) == {"Authorization": "Bearer tok"}
        return
    with pytest.raises(ValueError, match="NIL_SERVICE_KEY"):
        cp_auth.auth_headers(env)


def test_vault_get_carries_service_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"value": "https://odoo.example"}

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> _Resp:
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr("odoo_nil_adapter.tenant_routing.httpx.get", fake_get)

    client = VaultResolvingClient(
        "https://registry.example",
        headers=cp_auth.auth_headers({"NIL_SERVICE_KEY": f"{VECTOR_KID}:{VECTOR_SECRET}"}),
    )
    client._fetch_secret("ws1", "odoo_url")

    assert captured["headers"] == {"Authorization": VECTOR_HEADER}


def test_vault_get_falls_back_to_registry_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 404

        def json(self) -> dict[str, Any]:
            return {}

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> _Resp:
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr("odoo_nil_adapter.tenant_routing.httpx.get", fake_get)

    client = VaultResolvingClient(
        "https://registry.example",
        headers=cp_auth.auth_headers({"NIL_REGISTRY_TOKEN": "tok"}),
    )
    client._fetch_secret("ws1", "odoo_url")

    assert captured["headers"] == {"Authorization": "Bearer tok"}


def test_vault_get_sends_no_authorization_header_when_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 404

        def json(self) -> dict[str, Any]:
            return {}

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> _Resp:
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr("odoo_nil_adapter.tenant_routing.httpx.get", fake_get)

    client = VaultResolvingClient("https://registry.example", headers=cp_auth.auth_headers({}))
    client._fetch_secret("ws1", "odoo_url")

    assert captured["headers"] == {}
