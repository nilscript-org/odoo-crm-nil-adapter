"""crm.find_contact / crm.get_contact — the comms module's contact resolver.

Drives the edge's /query with the two new verbs against the in-memory FakeSystem: a client is
resolved to their channel card (email + phone) by free text, and one contact is fetched by id.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem


def _seeded() -> FakeSystem:
    sys = FakeSystem()
    sys.docs["res.partner"] = [
        {"id": 17, "name": "Client X", "phone": "+974501234567", "email": "buyer@clientx.com"},
        {"id": 18, "name": "Al-Faisal Trading", "phone": "+974509999999", "email": "ops@faisal.sa"},
    ]
    return sys


def _client(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _query(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/query", json=env).json()


def test_find_contact_returns_channel_card() -> None:
    client = _client(_seeded())
    out = _query(client, "crm.find_contact", {"query": "Client X"})
    data = out["data"]
    assert data["target"] == "res.partner"
    match = next((r for r in data["items"] if r.get("name") == "Client X"), None)
    assert match is not None
    assert match["email"] == "buyer@clientx.com"
    assert match["phone"] == "+974501234567"


def test_find_contact_blank_query_lists_contacts() -> None:
    client = _client(_seeded())
    out = _query(client, "crm.find_contact", {"query": ""})
    assert out["data"]["count"] >= 2  # a blank query lists, never errors


def test_get_contact_by_id() -> None:
    client = _client(_seeded())
    out = _query(client, "crm.get_contact", {"contact_id": "17"})
    assert out["data"]["found"] is True
    assert out["data"]["item"]["email"] == "buyer@clientx.com"


def test_get_contact_missing_or_bad_id_is_empty_not_error() -> None:
    client = _client(_seeded())
    assert _query(client, "crm.get_contact", {"contact_id": "9999"})["data"]["found"] is False
    assert _query(client, "crm.get_contact", {"contact_id": "abc"})["data"]["found"] is False
    assert _query(client, "crm.get_contact", {})["data"]["found"] is False
