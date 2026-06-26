"""Phase 4 — cross-module conformance. ONE representative model from each Odoo module group proves the
universal machinery covers it: discovery + lean projection + count + aggregate (reads), default-deny
then governed write, default-deny then governed method, and per-field sensitivity redaction. Synthetic
backend (FakeSystem) — a real-instance run is the remaining gap, logged in the plan, not faked here.

Module groups exercised: finance (account.move), inventory (stock.picking), sales (sale.order),
hr (hr.employee), product (product.product).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from odoo_crm_nil_adapter import governance
from odoo_crm_nil_adapter.edge import CapturingEmitter, create_app
from odoo_crm_nil_adapter.system import FakeSystem

TENANT = "ws_acme"


@pytest.fixture(autouse=True)
def _reset() -> None:
    governance.reset_policy()
    yield
    governance.reset_policy()


def _scalar(name: str, typ: str = "char") -> dict:
    return {"name": name, "type": typ, "required": False, "readonly": False}


# field catalogs (fields_get shape) + a couple of rows per module-group model
_MODELS: dict[str, dict] = {
    "account.move": {
        "schema": [_scalar("id", "integer"), _scalar("name"), _scalar("amount_total", "monetary"),
                   _scalar("state", "selection"), _scalar("narration", "text")],
        "rows": [{"id": i, "name": f"INV/{i}", "amount_total": 100 * i, "state": "draft",
                  "narration": "x" * 4000} for i in range(1, 4)],
        "group_by": "state",
    },
    "stock.picking": {
        "schema": [_scalar("id", "integer"), _scalar("name"), _scalar("state", "selection")],
        "rows": [{"id": i, "name": f"WH/OUT/{i}", "state": "assigned"} for i in range(1, 4)],
        "group_by": "state",
    },
    "sale.order": {
        "schema": [_scalar("id", "integer"), _scalar("name"), _scalar("amount_total", "monetary"),
                   _scalar("state", "selection")],
        "rows": [{"id": i, "name": f"S{i:05d}", "amount_total": 250 * i, "state": "draft"}
                 for i in range(1, 4)],
        "group_by": "state",
    },
    "hr.employee": {
        "schema": [_scalar("id", "integer"), _scalar("name"), _scalar("department_id", "many2one"),
                   _scalar("wage", "monetary")],  # `wage` → sensitive by classification
        "rows": [{"id": i, "name": f"Emp {i}", "department_id": [1, "Sales"], "wage": 9000 + i}
                 for i in range(1, 4)],
        "group_by": "department_id",
    },
    "product.product": {
        "schema": [_scalar("id", "integer"), _scalar("name"), _scalar("list_price", "monetary"),
                   _scalar("type", "selection")],
        "rows": [{"id": i, "name": f"Product {i}", "list_price": 10 * i, "type": "consu"}
                 for i in range(1, 4)],
        "group_by": "type",
    },
}


def _backend() -> FakeSystem:
    sys = FakeSystem()
    for model, spec in _MODELS.items():
        sys.schemas[model] = spec["schema"]
        sys.docs[model] = [dict(r) for r in spec["rows"]]
    return sys


def _client(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _query(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": TENANT, "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/query", json=env).json()["data"]


def _propose(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": TENANT, "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/propose", json=env).json()["body"]


def _commit(client: TestClient, pid: str) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": TENANT,
           "body": {"proposal": pid, "idempotency_key": pid}}
    return client.post("/nil/v0.1/commit", json=env).json()["body"]


# ── reads cover every module group (discovery + projection + count + aggregate) ────────────────────
@pytest.mark.parametrize("model", list(_MODELS))
def test_reads_work_across_module_groups(model: str) -> None:
    client = _client(_backend())
    assert _query(client, "nil.count", {"target": model}) == {"count": 3}
    search = _query(client, "nil.search", {"target": model, "limit": 10})
    assert len(search["items"]) == 3
    assert "narration" not in search["items"][0]  # heavy text dropped by the derived projection
    agg = _query(client, "nil.aggregate", {"target": model, "group_by": _MODELS[model]["group_by"]})
    assert sum(b["count"] for b in agg["groups"]) == 3


# ── writes are default-deny across module groups, governed once granted ───────────────────────────
def test_writes_default_deny_then_governed_per_module() -> None:
    client = _client(_backend())
    # default-deny on a financial model
    assert _propose(client, "resource.create", {"target": "account.move", "data": {"x": 1}})["outcome"] == "refusal"
    # operator grants product.product create for this tenant → now committable at the granted tier
    governance.grant_write(TENANT, "product.product", "create", "MEDIUM")
    body = _propose(client, "resource.create", {"target": "product.product", "data": {"name": "Widget"}})
    assert body["outcome"] == "proposal" and body["tier"] == "MEDIUM", body
    assert _commit(client, body["id"])["state"] == "executed"


def test_destructive_financial_write_escalates_to_critical() -> None:
    client = _client(_backend())
    governance.grant_write(TENANT, "account.move", "delete", "HIGH")  # operator grant…
    body = _propose(client, "resource.delete", {"target": "account.move", "id": 1})
    assert body["outcome"] == "proposal" and body["tier"] == "CRITICAL", body  # …escalated by policy


# ── methods are default-deny across module groups, governed once granted ──────────────────────────
def test_method_default_deny_then_governed_per_module() -> None:
    sys = _backend()
    client = _client(sys)
    assert _propose(client, "resource.method",
                    {"target": "stock.picking", "id": 1, "method": "button_validate"})["outcome"] == "refusal"
    governance.grant_method(TENANT, "stock.picking", "button_validate", "HIGH")
    body = _propose(client, "resource.method", {"target": "stock.picking", "id": 1, "method": "button_validate"})
    assert body["outcome"] == "proposal" and body["tier"] == "HIGH", body
    assert _commit(client, body["id"])["state"] == "executed"
    assert ("stock.picking", "1", "button_validate", {}) in sys.method_calls


# ── per-field sensitivity: an HR wage is redacted from discovery unless explicitly revealed ────────
def test_sensitive_field_redacted_by_default() -> None:
    client = _client(_backend())
    out = _query(client, "nil.search", {"target": "hr.employee", "fields": ["name", "wage"], "limit": 5})
    assert "wage" not in out["items"][0], out  # classified sensitive → dropped without a grant
    assert out.get("redacted") == ["wage"]  # dropped honestly, never silently
    revealed = _query(client, "nil.search",
                      {"target": "hr.employee", "fields": ["name", "wage"], "reveal": ["wage"], "limit": 5})
    assert "wage" in revealed["items"][0]  # explicit reveal grant returns it
