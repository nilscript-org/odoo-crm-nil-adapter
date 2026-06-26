"""Odoo full-coverage (Phase 1): the universal machinery reads ANY discovered Odoo model — not just
the curated CRM targets — while WRITES stay governed by a default-deny policy. Three pillars:

  A. Dynamic discovery — `describe_target` derives a lean, schema-driven projection for any model the
     instance exposes (via `fields_get`/`schema()`), so `nil.*` reads work over account.move,
     stock.picking, hr.employee, … with no hand-curated field list.
  B. Governance — lifting the READ ceiling does NOT lift the WRITE ceiling. `resource.*` writes are
     allowed only where policy grants them (default-deny financial/HR/undeclared), so the
     unexpressibility invariant (β⁻¹(a)=∅ for undeclared writes) still holds by construction.
  C. Generic `op="method"` — workflow actions (action_post, button_validate, …) are callable through a
     generic method verb, gated by a per-(model, method) allow-list (default-deny, no arbitrary call).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_crm_nil_adapter.edge import CapturingEmitter, create_app
from odoo_crm_nil_adapter.system import FakeSystem


def _client(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _query(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/query", json=env).json()


# A field catalog shaped like Odoo `fields_get` output (see system._field_meta): a heavy model with a
# mix of scalars, a selection, a relation, and a huge text field that a lean projection must drop.
_ACCOUNT_MOVE_SCHEMA = [
    {"name": "id", "type": "integer", "required": False, "readonly": True},
    {"name": "name", "type": "char", "required": False, "readonly": False},
    {"name": "amount_total", "type": "monetary", "required": False, "readonly": True},
    {"name": "state", "type": "selection", "required": False, "readonly": False,
     "options": [{"value": "draft", "label": "Draft"}, {"value": "posted", "label": "Posted"}]},
    {"name": "partner_id", "type": "many2one", "required": False, "readonly": False,
     "relation": "res.partner"},
    {"name": "narration", "type": "text", "required": False, "readonly": False},  # heavy → dropped
    {"name": "line_ids", "type": "one2many", "required": False, "readonly": False,
     "relation": "account.move.line"},  # collection → dropped
]


def _seeded_account_move() -> FakeSystem:
    sys = FakeSystem()
    sys.schemas["account.move"] = _ACCOUNT_MOVE_SCHEMA
    sys.docs["account.move"] = [
        {"id": i, "name": f"INV/2026/{i:04d}", "amount_total": 100 * i, "state": "posted",
         "partner_id": [7, "Acme"], "narration": "x" * 5000, "line_ids": [1, 2, 3]}
        for i in range(1, 6)
    ]
    return sys


# ── A. dynamic discovery: reads work over a model with NO curated _TARGET_FIELDS entry ────────────
def test_search_discovers_uncurated_model_and_projects_lean() -> None:
    client = _client(_seeded_account_move())
    out = _query(client, "nil.search", {"target": "account.move", "limit": 50})
    items = out["data"]["items"]
    assert len(items) == 5, out
    keys = set(items[0].keys())
    assert "id" in keys and "name" in keys and "amount_total" in keys  # lean scalars kept
    assert "narration" not in keys and "line_ids" not in keys  # heavy/collection fields dropped


def test_count_works_on_discovered_model() -> None:
    client = _client(_seeded_account_move())
    out = _query(client, "nil.count", {"target": "account.move"})
    assert out["data"] == {"count": 5}


def test_undiscoverable_model_still_refuses_cleanly() -> None:
    # A model the instance does not provision (schema → empty/None) is a clean refusal, never a 500.
    client = _client(FakeSystem())  # no schema seeded
    out = _query(client, "nil.search", {"target": "nonexistent.model", "limit": 10})
    assert out["data"].get("outcome") == "refused", out


def _propose(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/propose", json=env).json()["body"]


# ── B. governance: lifting the READ ceiling does not lift the WRITE ceiling ───────────────────────
def test_discovered_model_reads_but_writes_stay_denied() -> None:
    sys = _seeded_account_move()
    client = _client(sys)
    # READ is now universal — account.move (no curated entry) counts fine.
    assert _query(client, "nil.count", {"target": "account.move"})["data"] == {"count": 5}
    # WRITE is still default-denied by governance — a financial model is unexpressible for writes.
    body = _propose(client, "resource.create", {"target": "account.move", "data": {"amount_total": 999}})
    assert body["outcome"] == "refusal", body
    assert sys.docs.get("account.move.created") is None  # nothing forged on a sibling key
    assert len(sys.docs["account.move"]) == 5  # no row added by the refused write


def _commit(client: TestClient, pid: str) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "w",
           "body": {"proposal": pid, "idempotency_key": pid}}
    return client.post("/nil/v0.1/commit", json=env).json()["body"]


# ── C. generic op=method: governed workflow actions, no per-action verb ───────────────────────────
def test_generic_method_allowed_invokes_through_propose_commit() -> None:
    sys = FakeSystem()
    sys.docs["res.partner"] = [{"id": 5, "name": "Acme"}]
    client = _client(sys)
    body = _propose(client, "resource.method",
                    {"target": "res.partner", "id": 5, "method": "message_post",
                     "params": {"body": "hello from NIL"}})
    assert body["outcome"] == "proposal", body
    assert body["tier"] == "MEDIUM"
    status = _commit(client, body["id"])
    assert status["state"] == "executed", status
    assert sys.method_calls == [("res.partner", "5", "message_post", {"body": "hello from NIL"})]
    assert sys.messages[("res.partner", "5")] == ["hello from NIL"]


def test_generic_method_not_allowlisted_is_default_denied() -> None:
    sys = _seeded_account_move()
    client = _client(sys)
    # action_post on account.move is a financial workflow method — NOT in the method allow-list.
    body = _propose(client, "resource.method",
                    {"target": "account.move", "id": 1, "method": "action_post"})
    assert body["outcome"] == "refusal", body
    assert sys.method_calls == []  # never invoked — default-deny holds for methods too


def test_method_verb_is_advertised_in_describe() -> None:
    client = _client(FakeSystem())
    verbs = client.get("/nil/v0.1/describe").json()["verbs"]
    assert "resource.method" in verbs
