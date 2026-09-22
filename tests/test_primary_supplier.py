"""W3.6a (owner Q3): "primary supplier" is an attribute of the product<->supplier LINK, written by
the link's own authority — never a second copy in the platform. The vendor-neutral verb contract is
`procurement.set_primary_supplier {link_ref, priority, sku?}`; `priority` is an integer, lower =
preferred. On Odoo the link IS `product.supplierinfo`, and Odoo's own native ordering field for that
model is `sequence` (ascending) — so this verb writes `sequence` and the read side exposes it back
out as `priority` (never `sequence`, per the generality rule: the catalogue says "priority", never
the native field name).

`fake_client` here is a REAL `FakeSystem` (as in `test_supplier_link.py`), so these tests exercise
genuine search/update logic through the real edge — the before-image restore is proven by an actual
rollback, not a mock's say-so.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from odoo_nil_adapter import compensation, translate
from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem


@pytest.fixture
def fake_client() -> FakeSystem:
    return FakeSystem()


def _client(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _env(verb: str, args: dict) -> dict:
    return {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}


def _commit(client: TestClient, verb: str, args: dict) -> dict:
    pid = client.post("/nil/v0.1/propose", json=_env(verb, args)).json()["body"]["id"]
    return client.post(
        "/nil/v0.1/commit",
        json={"nil": "0.1", "grant": "g", "workspace": "w",
              "body": {"proposal": pid, "idempotency_key": pid}},
    ).json()["body"]


def _query(client: TestClient, verb: str, args: dict) -> dict:
    return client.post("/nil/v0.1/query", json=_env(verb, args)).json()["data"]


def _link(client: TestClient, product_ref: str = "201", supplier_ref: str = "7") -> str:
    linked = _commit(client, "procurement.link_supplier", {"product_ref": product_ref, "supplier_ref": supplier_ref})
    return linked["result"]["entity"]["id"]


# ── the verb is declared, on the wire, and writes with op="update" ───────────────────────────────
def test_describe_lists_set_primary_supplier_as_an_update(fake_client: FakeSystem) -> None:
    client = _client(fake_client)
    d = client.get("/nil/v0.1/describe").json()
    assert "procurement.set_primary_supplier" in d["verbs"]
    rows = {row["verb"]: row for row in d["verb_details"]}
    assert rows["procurement.set_primary_supplier"]["target"] == "product.supplierinfo"


def test_set_primary_supplier_is_declared_update_op() -> None:
    assert translate.PROCUREMENT_SET_PRIMARY_SUPPLIER.op == "update"
    assert translate.PROCUREMENT_SET_PRIMARY_SUPPLIER.doctype == "product.supplierinfo"
    assert translate.PROCUREMENT_SET_PRIMARY_SUPPLIER.required == ("link_ref", "priority")


# ── to_native is a pure priority -> sequence mapping (no sibling read) ───────────────────────────
def test_to_native_maps_priority_to_sequence() -> None:
    native = translate.PROCUREMENT_SET_PRIMARY_SUPPLIER.to_native({"link_ref": "42", "priority": "3"})
    assert native == {"sequence": 3}


# ── the update path records a before-image, so COMPENSABLE is honest ─────────────────────────────
def test_set_primary_supplier_is_compensable_via_before_image() -> None:
    assert compensation.COMPENSATIONS["procurement.set_primary_supplier"] == {
        "reversibility": "COMPENSABLE",
        "strategy": "before_image",
    }


def test_set_primary_supplier_rollback_restores_the_before_image_priority(fake_client: FakeSystem) -> None:
    client = _client(fake_client)
    link_ref = _link(client)
    _commit(client, "procurement.set_primary_supplier", {"link_ref": link_ref, "priority": "5"})

    committed = _commit(client, "procurement.set_primary_supplier", {"link_ref": link_ref, "priority": "1"})
    comp = committed.get("compensation", {})
    assert comp.get("reversibility") == "COMPENSABLE", f"expected COMPENSABLE, got {comp}"
    token = comp.get("token")
    assert token, "a COMPENSABLE update must mint a rollback token"

    rolled = client.post("/nil/v0.1/rollback", json={
        "nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": token, "reason": "owner_cancel"},
    }).json()["body"]
    assert rolled["outcome"] == "proposal", "ROLLBACK previews a restore, never silently writes"
    assert rolled["resolved"]["data"]["sequence"] == 5, "rollback must restore the PRIOR priority (5), not 1"


# ── convergence: setting the same priority twice is one state, no drift ──────────────────────────
def test_set_primary_supplier_is_convergent_on_replay(fake_client: FakeSystem) -> None:
    client = _client(fake_client)
    link_ref = _link(client)

    a = _commit(client, "procurement.set_primary_supplier", {"link_ref": link_ref, "priority": "2"})
    b = _commit(client, "procurement.set_primary_supplier", {"link_ref": link_ref, "priority": "2"})
    assert a["state"] == "executed" and b["state"] == "executed"
    assert fake_client.get("product.supplierinfo", link_ref)["sequence"] == 2


# ── the read side exposes the native `sequence` back out as `priority` — never the native name ───
def test_primary_supplier_projection_carries_priority(fake_client: FakeSystem) -> None:
    client = _client(fake_client)
    link_ref = _link(client)
    _commit(client, "procurement.set_primary_supplier", {"link_ref": link_ref, "priority": "9"})

    out = _query(client, "nil.search", {"target": "ProductSupplier"})

    assert out["items"][0]["priority"] == 9


def test_primary_supplier_read_projection_is_a_pure_helper(fake_client: FakeSystem) -> None:
    """Unit-level companion to the end-to-end projection test above: `_project_product_supplier_row`
    itself must add `priority` from a raw `sequence` field, independent of the write path."""
    row = translate._project_product_supplier_row(
        fake_client, "ProductSupplier", {"id": 1, "partner_id": 7, "sequence": 4}
    )
    assert row["priority"] == 4
