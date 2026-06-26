"""The read data plane on the Odoo shim: lean, filtered, projected reads — the fix for the 590 KB
flood. Drives the edge's /query with the canonical `nil.*` verbs against the in-memory FakeSystem,
proving search projects (id/name/phone, never the whole res.partner), count is O(1)-shaped, and a
name filter finds the row — on 41 contacts the same way it would on 1,000,000.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from odoo_crm_nil_adapter.edge import CapturingEmitter, create_app
from odoo_crm_nil_adapter.system import FakeSystem


def _seeded(n: int) -> FakeSystem:
    sys = FakeSystem()
    # res.partner rows carry MANY fields (like a real Odoo dump) — the projection must drop all but
    # the lean set. One row is رغد عبدالله so the name filter has something to find.
    sys.docs["res.partner"] = [
        {
            "id": i,
            "name": "رغد عبدالله" if i == 17 else f"Contact {i}",
            "phone": f"+9745{i:07d}",
            "email": f"c{i}@example.com",
            "credit_limit": 5000 + i,
            "comment": "x" * 400,
            "category_id": [1, 2, 3],
        }
        for i in range(n)
    ]
    return sys


def _client(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _query(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/query", json=env).json()


def test_search_projects_to_lean_fields_not_whole_records() -> None:
    client = _client(_seeded(41))
    out = _query(client, "nil.search", {"target": "res.partner", "fields": ["name", "phone"], "limit": 50})
    item = out["data"]["items"][0]
    assert set(item.keys()) == {"id", "name", "phone"}  # NOT credit_limit/comment/category_id
    # the whole page must be small — the 590 KB flood is gone.
    assert len(json.dumps(out, ensure_ascii=False).encode()) < 50_000


def test_search_filters_by_name_and_finds_the_row() -> None:
    client = _client(_seeded(41))
    out = _query(
        client, "nil.search",
        {"target": "res.partner", "filter": [{"field": "name", "op": "ilike", "value": "رغد"}],
         "fields": ["name"]},
    )
    items = out["data"]["items"]
    assert len(items) == 1 and items[0]["name"] == "رغد عبدالله"


def test_count_returns_just_a_count() -> None:
    client = _client(_seeded(41))
    out = _query(client, "nil.count", {"target": "res.partner"})
    assert out["data"] == {"count": 41}


def test_get_returns_one_lean_record_by_id() -> None:
    client = _client(_seeded(41))
    out = _query(client, "nil.get", {"target": "res.partner", "id": 17, "fields": ["name"]})
    assert out["data"] == {"id": 17, "name": "رغد عبدالله"}
