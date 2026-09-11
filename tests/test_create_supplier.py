"""Task 1.3b: `crm.create_contact` can create a SUPPLIER too — `procurement.create_supplier` writes
the same `res.partner` model, stamped `supplier_rank: 1` (a vendor, never a customer) instead of the
plain contact create_contact performs. It routes here whenever `ManageSuppliers.create` (the platform
baseline capability) resolves to Odoo as the Supplier authority.

`fake_client` here is a REAL `FakeSystem` (not the minimal recording double in conftest.py) so these
tests exercise genuine search/create logic through the real edge — convergence is proven by an actual
absent second write, not a mock's say-so. Mirrors `tests/test_supplier_link.py`'s harness exactly.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from odoo_nil_adapter import compensation, translate
from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem


class _CountingFakeSystem(FakeSystem):
    """FakeSystem, plus a per-target create counter — proves a converged second call wrote nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.created: dict[str, int] = {}

    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        self.created[target] = self.created.get(target, 0) + 1
        return super().create(target, doc)


@pytest.fixture
def fake_client() -> _CountingFakeSystem:
    return _CountingFakeSystem()


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


# ── create maps to supplier_rank: 1 and is_company: True ─────────────────────────────────────────
def test_create_maps_to_supplier_rank_and_is_company(fake_client: _CountingFakeSystem) -> None:
    client = _client(fake_client)
    committed = _commit(
        client, "procurement.create_supplier",
        {"name": "Acme Vendor Co", "email": "vendor@acme.example", "phone": "0501234567", "vat": "SA123"},
    )
    assert committed["state"] == "executed"
    record = fake_client.docs["res.partner"][0]
    assert record["supplier_rank"] == 1
    assert record["is_company"] is True
    # customer_rank is left UNTOUCHED — never written, so this is a vendor and nothing else.
    assert "customer_rank" not in record
    assert record["name"] == "Acme Vendor Co"
    assert record["email"] == "vendor@acme.example"
    assert record["phone"] == "0501234567"
    assert record["vat"] == "SA123"


def test_optional_args_are_omitted_when_not_given(fake_client: _CountingFakeSystem) -> None:
    client = _client(fake_client)
    _commit(client, "procurement.create_supplier", {"name": "Bare Vendor"})
    record = fake_client.docs["res.partner"][0]
    assert record["is_company"] is True
    assert record["supplier_rank"] == 1
    assert "email" not in record
    assert "phone" not in record
    assert "vat" not in record


# ── convergence: a second create with the same email returns the existing id, no second write ────
def test_convergent_on_email_second_create_is_a_no_op(fake_client: _CountingFakeSystem) -> None:
    # `name` is held IDENTICAL across both calls on purpose: `res.partner.name` is also this
    # FakeSystem's record identity (it has no separate numeric id), so varying it would make the
    # SECOND call's own write shift what `entity.id` reads back as — a fake-harness artifact, not a
    # dedup question. Convergence is proven the same way test_supplier_link.py proves it: an
    # unchanged entity id AND exactly one create.
    client = _client(fake_client)
    first = _commit(
        client, "procurement.create_supplier", {"name": "Acme Vendor Co", "email": "vendor@acme.example"},
    )
    second = _commit(
        client, "procurement.create_supplier",
        {"name": "Acme Vendor Co", "email": "vendor@acme.example", "phone": "0501234567"},
    )
    assert first["state"] == "executed" and second["state"] == "executed"
    assert first["result"]["entity"]["id"] == second["result"]["entity"]["id"]
    assert fake_client.created["res.partner"] == 1
    # SET semantics: the second call's new field lands on the SAME record, proving it converged
    # onto an update rather than silently discarding the retry's payload.
    assert fake_client.docs["res.partner"][0]["phone"] == "0501234567"


def test_convergent_on_name_when_no_email_given(fake_client: _CountingFakeSystem) -> None:
    """No email at all — the fallback dedup key (`name`) converges a retry instead of minting a
    second vendor purely because the caller supplied no email either time."""
    client = _client(fake_client)
    first = _commit(client, "procurement.create_supplier", {"name": "Nameonly Vendor"})
    second = _commit(client, "procurement.create_supplier", {"name": "Nameonly Vendor"})
    assert first["result"]["entity"]["id"] == second["result"]["entity"]["id"]
    assert fake_client.created["res.partner"] == 1


# ── compensation: REVERSIBLE, reusing crm.delete_contact (same res.partner, same reversal) ────────
def test_compensation_is_reversible_via_delete_contact() -> None:
    spec = compensation.COMPENSATIONS["procurement.create_supplier"]
    assert spec["reversibility"] == "REVERSIBLE"
    assert spec["verb"] == "crm.delete_contact"


def test_rollback_previews_the_delete(fake_client: _CountingFakeSystem) -> None:
    client = _client(fake_client)
    committed = _commit(client, "procurement.create_supplier", {"name": "Acme Vendor Co"})
    token = committed["compensation"]["token"]

    rolled = client.post("/nil/v0.1/rollback", json={
        "nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": token, "reason": "owner_cancel"},
    }).json()["body"]

    assert rolled["outcome"] == "proposal"
    assert rolled["verb"] == "crm.delete_contact"
    assert rolled["resolved"]["contact_id"] == committed["result"]["entity"]["id"]


# ── describe: the verb is listed and advertised as committable ────────────────────────────────────
def test_describe_lists_the_verb() -> None:
    client = _client(FakeSystem())
    d = client.get("/nil/v0.1/describe").json()
    assert "procurement.create_supplier" in d["verbs"]
    rows = {row["verb"]: row for row in d["verb_details"]}
    row = rows["procurement.create_supplier"]
    assert row["reversibility"] == "REVERSIBLE"
    assert row["target"] == "res.partner"
    assert row["witness"]["shape"] == "convergent"
    assert row["witness"]["identity"]


# ── read-after-write: a Supplier read (fake plane) finds the created record ───────────────────────
def test_supplier_read_finds_the_created_record(fake_client: _CountingFakeSystem) -> None:
    client = _client(fake_client)
    _commit(
        client, "procurement.create_supplier",
        {"name": "Findable Vendor", "email": "findable@acme.example"},
    )

    out = translate._run_nil_search(
        fake_client,
        {"target": "Supplier", "filter": [{"field": "email", "op": "eq", "value": "findable@acme.example"}]},
    )

    assert len(out["items"]) == 1
    assert out["items"][0]["name"] == "Findable Vendor"


def test_supplier_read_excludes_a_plain_contact(fake_client: _CountingFakeSystem) -> None:
    """A plain `crm.create_contact` (no supplier_rank) must NOT show up under a `Supplier` read — the
    base domain (`supplier_rank > 0`) is what makes the two resources distinct on one shared model."""
    client = _client(fake_client)
    _commit(client, "crm.create_contact", {"name": "Just A Contact", "email": "contact@acme.example"})

    out = translate._run_nil_search(
        fake_client,
        {"target": "Supplier", "filter": [{"field": "email", "op": "eq", "value": "contact@acme.example"}]},
    )

    assert out["items"] == []
