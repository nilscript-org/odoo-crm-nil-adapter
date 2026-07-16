"""The single intent payload on the Odoo shim: POST a semantic Intent to /query (verb nil.intent) and
the adapter deterministically resolves it via IntentResolver over the ReadPlane. No verb selection by
the caller — one payload, the system executes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem


def _seeded() -> FakeSystem:
    sys = FakeSystem()
    sys.docs["res.partner"] = [
        {"id": i, "name": ("دينا كمال النجار" if i == 18 else f"Contact {i}"),
         "phone": f"+9745{i:07d}", "email": f"c{i}@x.com", "junk": "z" * 300}
        for i in range(41)
    ]
    return sys


def _client(sys):
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _intent(client, intent):
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": "nil.intent", "args": intent}}
    return client.post("/nil/v0.1/query", json=env).json()


def test_intent_seek_the_finds_one_lean_record() -> None:
    out = _intent(_client(_seeded()), {
        "about": "res.partner",
        "where": [{"attr": "name", "rel": "contains", "value": "دينا"}],
        "seek": "the",
    })["data"]
    assert out["outcome"] == "result"
    assert out["value"]["name"] == "دينا كمال النجار"
    assert set(out["value"].keys()) == {"id", "name", "phone", "email"}  # lean, not whole record


def test_intent_seek_count() -> None:
    out = _intent(_client(_seeded()), {"about": "res.partner", "where": [], "seek": "count"})["data"]
    assert out == {"outcome": "result", "value": {"count": 41}}


def test_intent_unknown_about_is_structured_refusal() -> None:
    out = _intent(_client(_seeded()), {"about": "hr.salary", "where": [], "seek": "count"})["data"]
    assert out["outcome"] == "refused" and out["code"]


def test_intent_resolves_business_resource_name_to_native_model() -> None:
    # The agent speaks BUSINESS: about="Product". Odoo stores it as `product.product`. The adapter
    # DECLARES that mapping in RESOURCES; it must APPLY it on reads, not pass "Product" through as a
    # model name (which Odoo has no table for → CAPABILITY_UNSUPPORTED). This is the exact gap that
    # made "اعرض لي المنتجات" refuse in production while `about="res.partner"` worked.
    sys = _seeded()
    sys.docs["product.product"] = [{"id": i, "name": f"SKU-{i}"} for i in range(4)]
    out = _intent(_client(sys), {"about": "Product", "where": [], "seek": "count"})["data"]
    assert out == {"outcome": "result", "value": {"count": 4}}


def test_intent_native_model_name_still_passes_through() -> None:
    # Translation must not break the native spelling: an agent that says `product.product` (or learned
    # it from describe) still lands on the same table. Business name and native name are one resource.
    sys = _seeded()
    sys.docs["product.product"] = [{"id": i, "name": f"SKU-{i}"} for i in range(4)]
    out = _intent(_client(sys), {"about": "product.product", "where": [], "seek": "count"})["data"]
    assert out == {"outcome": "result", "value": {"count": 4}}
