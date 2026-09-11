"""Task 1.3 (D37/D38/O3): the product<->supplier link is a resource Odoo reads and a verb Odoo
writes. `procurement.link_supplier` / `unlink_supplier` / `link_suppliers` are a direct-execution
surface (`translate.run`) — see that function's docstring in translate.py for why they bypass the
generic WRITE_VERBS/edge.py COMMIT spine (compound-pair convergence + a live sku lookup, neither of
which the closed-for-editing `edge.py` can express).

`fake_client` here is a REAL `FakeSystem` (not the minimal recording double in conftest.py) so these
tests exercise genuine search/create/delete logic — convergence is proven by actually not writing a
second record, not by mocking a client that says so.
"""

from __future__ import annotations

from typing import Any

import pytest

from odoo_nil_adapter import compensation, translate
from odoo_nil_adapter.system import FakeSystem


class _CountingFakeSystem(FakeSystem):
    """FakeSystem, plus a per-target create counter — the exact `fake_client.created[...]` surface
    the brief's own test asserts on, to prove a converged second call wrote nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.created: dict[str, int] = {}

    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        self.created[target] = self.created.get(target, 0) + 1
        return super().create(target, doc)


@pytest.fixture
def fake_client() -> _CountingFakeSystem:
    sys = _CountingFakeSystem()
    sys.docs["product.product"] = [
        {"id": 101, "name": "Widget 16", "default_code": "ODOO-16", "product_tmpl_id": 201},
        {"id": 102, "name": "Widget 17", "default_code": "ODOO-17", "product_tmpl_id": 202},
    ]
    sys.docs["res.partner"] = [
        {"id": 7, "name": "Acme Supplies", "supplier_rank": 1},
        {"id": 8, "name": "Best Vendors", "supplier_rank": 1},
        {"id": 9, "name": "Pure Customer", "customer_rank": 1},  # NOT a supplier
    ]
    return sys


# ── the brief's three tests, verbatim ──────────────────────────────────────────────────────────
def test_link_is_convergent_on_the_pair(fake_client: _CountingFakeSystem) -> None:
    a = translate.run("procurement.link_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})
    b = translate.run("procurement.link_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})
    assert a["id"] == b["id"] and fake_client.created["product.supplierinfo"] == 1


def test_link_reversibility_is_declared() -> None:
    assert compensation.PRODUCT_SUPPLIER_COMPENSATIONS["procurement.link_supplier"]["reversibility"] == "REVERSIBLE"


def test_links_are_readable_as_a_resource(fake_client: _CountingFakeSystem) -> None:
    translate.run("procurement.link_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})
    out = translate._run_nil_search(
        fake_client,
        {"target": "ProductSupplier", "filter": [{"field": "sku", "op": "eq", "value": "ODOO-16"}]},
    )
    assert out["items"][0]["supplier_id"]


# ── additional covering tests ──────────────────────────────────────────────────────────────────
def test_links_are_readable_and_carry_the_sku_too(fake_client: _CountingFakeSystem) -> None:
    translate.run("procurement.link_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})
    out = translate._run_nil_search(fake_client, {"target": "ProductSupplier"})
    assert out["items"][0]["sku"] == "ODOO-16"
    assert out["items"][0]["supplier_id"] == "7"


def test_link_refuses_an_unknown_sku(fake_client: _CountingFakeSystem) -> None:
    out = translate.run("procurement.link_supplier", fake_client, {"sku": "NO-SUCH-SKU", "supplier_id": "7"})
    assert out["outcome"] == "refused"
    assert fake_client.created.get("product.supplierinfo", 0) == 0


def test_link_refuses_a_partner_that_is_not_a_supplier(fake_client: _CountingFakeSystem) -> None:
    # id 9 is a real partner, but customer_rank only — not supplier_rank > 0.
    out = translate.run("procurement.link_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "9"})
    assert out["outcome"] == "refused"
    assert fake_client.created.get("product.supplierinfo", 0) == 0


def test_unlink_removes_the_pair(fake_client: _CountingFakeSystem) -> None:
    linked = translate.run("procurement.link_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})
    out = translate.run("procurement.unlink_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})
    assert out["unlinked"] is True
    assert fake_client.get("product.supplierinfo", linked["id"]) is None


def test_unlink_of_an_already_unlinked_pair_is_a_convergent_no_op(fake_client: _CountingFakeSystem) -> None:
    out = translate.run("procurement.unlink_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})
    assert out["unlinked"] is False


def test_unlink_compensation_shape_is_a_recreate_from_the_before_image(
    fake_client: _CountingFakeSystem,
) -> None:
    translate.run(
        "procurement.link_supplier",
        fake_client,
        {"sku": "ODOO-16", "supplier_id": "7", "price": "12.5", "min_qty": "3", "delay_days": "5"},
    )
    unlinked = translate.run("procurement.unlink_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})

    spec = compensation.PRODUCT_SUPPLIER_COMPENSATIONS["procurement.unlink_supplier"]
    assert spec["reversibility"] == "COMPENSABLE" and spec["strategy"] == "before_image"

    comp_args = compensation.compensate_product_supplier_link("procurement.unlink_supplier", unlinked)
    assert comp_args == {
        "sku": "ODOO-16", "supplier_id": "7", "price": 12.5, "min_qty": 3.0, "delay_days": 5.0,
    }
    # re-running the compensating verb with those exact args must recreate the same effective link.
    recreated = translate.run("procurement.link_supplier", fake_client, comp_args)
    assert recreated["created"] is True
    assert fake_client.created["product.supplierinfo"] == 2  # one create, one unlink, one re-create


def test_batch_link_is_convergent_per_pair(fake_client: _CountingFakeSystem) -> None:
    translate.run("procurement.link_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})

    out = translate.run(
        "procurement.link_suppliers",
        fake_client,
        {"links": [
            {"sku": "ODOO-16", "supplier_id": "7"},  # already linked -> existing
            {"sku": "ODOO-17", "supplier_id": "8"},  # new -> linked
        ]},
    )

    assert len(out["linked"]) == 1 and len(out["existing"]) == 1
    assert fake_client.created["product.supplierinfo"] == 2  # the first call's + exactly one more


def test_batch_link_refusal_on_an_unknown_sku_creates_nothing(fake_client: _CountingFakeSystem) -> None:
    out = translate.run(
        "procurement.link_suppliers",
        fake_client,
        {"links": [
            {"sku": "ODOO-16", "supplier_id": "7"},   # valid
            {"sku": "GHOST-SKU", "supplier_id": "8"},  # unknown -> the whole batch refuses
        ]},
    )

    assert out["outcome"] == "refused"
    assert fake_client.created.get("product.supplierinfo", 0) == 0


def test_batch_reversibility_is_declared_and_unlinks_only_what_it_created(
    fake_client: _CountingFakeSystem,
) -> None:
    # a pre-existing link that the batch must NOT touch on rollback
    translate.run("procurement.link_supplier", fake_client, {"sku": "ODOO-16", "supplier_id": "7"})

    spec = compensation.PRODUCT_SUPPLIER_COMPENSATIONS["procurement.link_suppliers"]
    assert spec["reversibility"] == "REVERSIBLE" and spec["verb"] == "procurement.unlink_suppliers"

    committed = translate.run(
        "procurement.link_suppliers",
        fake_client,
        {"links": [
            {"sku": "ODOO-16", "supplier_id": "7"},  # pre-existing -> must survive the rollback
            {"sku": "ODOO-17", "supplier_id": "8"},  # freshly created -> must be undone
        ]},
    )
    comp_args = compensation.compensate_product_supplier_link("procurement.link_suppliers", committed)
    assert comp_args == {"links": [{"sku": "ODOO-17", "supplier_id": "8"}]}

    rolled_back = translate.run("procurement.unlink_suppliers", fake_client, comp_args)
    assert rolled_back["unlinked"]

    still_there = translate._run_nil_search(fake_client, {"target": "ProductSupplier"})
    remaining = {(item["sku"], item["supplier_id"]) for item in still_there["items"]}
    assert remaining == {("ODOO-16", "7")}  # the pre-existing pair, and only it, survives
