"""The single intent payload on the Odoo shim: POST a semantic Intent to /query (verb nil.intent) and
the adapter deterministically resolves it via IntentResolver over the ReadPlane. No verb selection by
the caller — one payload, the system executes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_crm_nil_adapter.edge import CapturingEmitter, create_app
from odoo_crm_nil_adapter.system import FakeSystem


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
