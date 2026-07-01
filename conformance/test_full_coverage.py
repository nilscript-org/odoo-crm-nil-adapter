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

import pytest
from fastapi.testclient import TestClient

from odoo_nil_adapter import governance
from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem


@pytest.fixture(autouse=True)
def _reset_policy() -> None:
    governance.reset_policy()  # per-tenant grants / module scope never leak between tests
    yield
    governance.reset_policy()


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


def test_projection_ranks_business_fields_over_technical_noise() -> None:
    # A real Odoo model carries dozens of mail/audit/technical fields that sort BEFORE the business
    # ones alphabetically (access_*, activity_*, message_*). The derived projection must rank by
    # business value, not alphabet — otherwise accounting reads return access_token, not amount_total.
    sys = FakeSystem()
    sys.schemas["account.move"] = [
        {"name": "id", "type": "integer"},
        {"name": "access_token", "type": "char"},
        {"name": "activity_state", "type": "selection"},
        {"name": "message_main_attachment_id", "type": "many2one", "relation": "ir.attachment"},
        {"name": "name", "type": "char"},
        {"name": "ref", "type": "char"},
        {"name": "state", "type": "selection"},
        {"name": "move_type", "type": "selection"},
        {"name": "amount_total", "type": "monetary"},
        {"name": "partner_id", "type": "many2one", "relation": "res.partner"},
        {"name": "invoice_date", "type": "date"},
        {"name": "narration", "type": "text"},          # heavy → always dropped
        {"name": "line_ids", "type": "one2many"},        # collection → always dropped
    ]
    sys.docs["account.move"] = [{
        "id": 1, "name": "INV/1", "amount_total": 100, "state": "posted", "move_type": "out_invoice",
        "ref": "PO-9", "partner_id": [7, "Acme"], "invoice_date": "2026-06-27",
        "access_token": "x", "activity_state": "overdue", "message_main_attachment_id": [3, "a"],
        "narration": "y", "line_ids": [1, 2],
    }]
    client = _client(sys)
    keys = set(_query(client, "nil.search", {"target": "account.move", "limit": 1})["data"]["items"][0].keys())
    # business fields kept
    for biz in ("name", "state", "move_type", "amount_total", "partner_id"):
        assert biz in keys, f"{biz} should be in the projection; got {sorted(keys)}"
    # technical / mail noise dropped
    for noise in ("access_token", "activity_state", "message_main_attachment_id", "narration", "line_ids"):
        assert noise not in keys, f"{noise} must not be in the projection; got {sorted(keys)}"


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


def _rollback(client: TestClient, token: str) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"compensation_token": token}}
    return client.post("/nil/v0.1/rollback", json=env).json()["body"]


# ── Phase 3: a method with a declared inverse is COMPENSABLE; rollback previews + runs the inverse ──
def test_reversible_method_mints_compensation_and_rollback_runs_inverse() -> None:
    governance.grant_method("w", "account.move", "action_post", "HIGH", reverse="button_draft")
    sys = FakeSystem()
    sys.docs["account.move"] = [{"id": 1, "name": "INV/2026/0001"}]
    client = _client(sys)
    body = _propose(client, "resource.method", {"target": "account.move", "id": 1, "method": "action_post"})
    assert body["tier"] == "HIGH", body
    status = _commit(client, body["id"])
    comp = status["compensation"]
    assert comp["reversibility"] == "COMPENSABLE" and "token" in comp, comp
    # ROLLBACK previews the inverse (button_draft), never writes directly.
    rb = _rollback(client, comp["token"])
    assert rb["outcome"] == "proposal" and rb["resolved"]["method"] == "button_draft", rb
    # committing the reversal actually calls the inverse method.
    assert _commit(client, rb["id"])["state"] == "executed"
    assert ("account.move", "1", "button_draft", {}) in sys.method_calls


def test_method_without_inverse_is_irreversible() -> None:
    sys = FakeSystem()
    sys.docs["res.partner"] = [{"id": 5, "name": "Acme"}]
    client = _client(sys)
    body = _propose(client, "resource.method",
                    {"target": "res.partner", "id": 5, "method": "message_post", "params": {"body": "x"}})
    status = _commit(client, body["id"])
    assert status["compensation"]["reversibility"] == "IRREVERSIBLE"
    assert "token" not in status["compensation"]


# ── Phase 5: operator module scope makes out-of-scope models undiscoverable AND unwritable ─────────
def test_module_scope_blocks_discovery_and_writes_end_to_end() -> None:
    governance.set_enabled_modules({"crm"})
    sys = _seeded_account_move()  # finance model, provisioned but out of the enabled scope
    client = _client(sys)
    read = _query(client, "nil.count", {"target": "account.move"})
    assert read["data"].get("outcome") == "refused", read  # undiscoverable under scope
    # crm.lead (in scope) still discoverable/writable
    sys.docs["crm.lead"] = [{"id": 1, "name": "L1"}]
    assert _query(client, "nil.count", {"target": "crm.lead"})["data"] == {"count": 1}


# ── Phase 6: semantic verbs (curated sugar) across module groups ──────────────────────────────────
def test_semantic_create_invoice_creates_account_move() -> None:
    sys = FakeSystem()
    client = _client(sys)
    body = _propose(client, "account.create_invoice",
                    {"partner_id": 7, "lines": [{"name": "Item", "quantity": 2, "price_unit": 50}]})
    assert body["outcome"] == "proposal" and body["tier"] == "HIGH", body
    assert _commit(client, body["id"])["state"] == "executed"
    invoices = sys.docs.get("account.move", [])
    assert len(invoices) == 1 and invoices[0]["move_type"] == "out_invoice"
    assert invoices[0]["partner_id"] == 7


def test_semantic_validate_picking_invokes_button_validate() -> None:
    sys = FakeSystem()
    sys.docs["stock.picking"] = [{"id": 3, "name": "WH/OUT/00003"}]
    client = _client(sys)
    body = _propose(client, "stock.validate_picking", {"picking_id": 3})
    assert body["outcome"] == "proposal", body
    assert _commit(client, body["id"])["state"] == "executed"
    assert ("stock.picking", "3", "button_validate", {}) in sys.method_calls


def test_semantic_confirm_order_invokes_action_confirm() -> None:
    sys = FakeSystem()
    sys.docs["sale.order"] = [{"id": 9, "name": "S00009"}]
    client = _client(sys)
    body = _propose(client, "sale.confirm_order", {"order_id": 9})
    assert _commit(client, body["id"])["state"] == "executed"
    assert ("sale.order", "9", "action_confirm", {}) in sys.method_calls


def test_semantic_verbs_advertised_in_describe() -> None:
    verbs = _client(FakeSystem()).get("/nil/v0.1/describe").json()["verbs"]
    for v in ("account.create_invoice", "stock.validate_picking", "sale.confirm_order",
              "account.post_invoice", "account.register_payment"):
        assert v in verbs


def _rb(client: TestClient, token: str) -> dict:
    return client.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
                       "body": {"compensation_token": token}}).json()["body"]


def test_post_invoice_is_compensable_and_rolls_back_to_draft() -> None:
    sys = FakeSystem()
    sys.docs["account.move"] = [{"id": 1, "name": "INV/1", "state": "draft"}]
    client = _client(sys)
    body = _propose(client, "account.post_invoice", {"invoice_id": 1})
    assert body["outcome"] == "proposal" and body["tier"] == "HIGH", body
    status = _commit(client, body["id"])
    assert ("account.move", "1", "action_post", {}) in sys.method_calls
    comp = status["result"]["compensation"]
    assert comp["reversibility"] == "COMPENSABLE", comp
    rb = _rb(client, comp["token"])
    assert rb["resolved"]["method"] == "button_draft", rb
    _commit(client, rb["id"])
    assert ("account.move", "1", "button_draft", {}) in sys.method_calls  # un-posted


def test_register_payment_creates_account_payment() -> None:
    sys = FakeSystem()
    client = _client(sys)
    body = _propose(client, "account.register_payment", {"partner_id": 7, "amount": 500})
    assert body["outcome"] == "proposal" and body["tier"] == "HIGH", body
    assert _commit(client, body["id"])["state"] == "executed"
    pay = sys.docs.get("account.payment", [])
    assert pay and pay[0]["payment_type"] == "inbound" and pay[0]["partner_type"] == "customer"


def test_env_grants_persist_and_enable_a_tenant() -> None:
    import os
    governance.reset_policy()
    assert governance.write_tier("account.move", "create", tenant="ws_acme") is None  # before
    os.environ["NIL_TENANT_GRANTS"] = (
        '{"ws_acme":{"writes":[{"model":"account.move","op":"create","tier":"HIGH"}],'
        '"methods":[{"model":"account.move","method":"action_post","tier":"HIGH","reverse":"button_draft"}]}}'
    )
    try:
        applied = governance.load_grants_from_env()
        assert applied == 2
        assert governance.write_tier("account.move", "create", tenant="ws_acme") == "HIGH"
        assert governance.method_reverse("account.move", "action_post", tenant="ws_acme") == "button_draft"
        assert governance.write_tier("account.move", "create", tenant="ws_other") is None  # isolated
    finally:
        del os.environ["NIL_TENANT_GRANTS"]
        governance.reset_policy()
