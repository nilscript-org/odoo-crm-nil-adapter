"""Task 1.3 (D37/D38/O3): the product<->supplier link is a resource Odoo reads and a verb Odoo
writes. Fix round 1 (coordinator ruling): a write not on the governed wire does not exist for this
platform — `procurement.link_supplier` / `procurement.unlink_supplier` are real `WRITE_VERBS`
entries, declared in `/nil/v0.1/describe`, committed through `edge.py`'s ordinary PROPOSE->COMMIT
like every other curated verb. `to_native` is PURE: `product_ref` (the product TEMPLATE's id) and
`supplier_ref` (the partner id) arrive pre-resolved from the caller (the control plane / os-server
hold both in the Odoo mirror).

`fake_client` here is a REAL `FakeSystem` (not the minimal recording double in conftest.py) so these
tests exercise genuine search/create/delete logic through the real edge — convergence is proven by
an actual absent second write, not a mock's say-so.
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


def _query(client: TestClient, verb: str, args: dict) -> dict:
    return client.post("/nil/v0.1/query", json=_env(verb, args)).json()["data"]


# ── the brief's three tests, adapted to the new arg names (product_ref/supplier_ref) ─────────────
def test_link_is_convergent_on_the_pair(fake_client: _CountingFakeSystem) -> None:
    client = _client(fake_client)
    a = _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})
    b = _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})
    assert a["state"] == "executed" and b["state"] == "executed"
    assert a["result"]["entity"]["id"] == b["result"]["entity"]["id"]
    assert fake_client.created["product.supplierinfo"] == 1


def test_link_reversibility_is_declared() -> None:
    assert compensation.COMPENSATIONS["procurement.link_supplier"]["reversibility"] == "REVERSIBLE"


def test_links_are_readable_as_a_resource(fake_client: _CountingFakeSystem) -> None:
    fake_client.docs["product.product"] = [
        {"id": 101, "name": "Widget 16", "default_code": "ODOO-16", "product_tmpl_id": 201},
    ]
    client = _client(fake_client)
    _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})

    out = translate._run_nil_search(
        fake_client,
        {"target": "ProductSupplier", "filter": [{"field": "sku", "op": "eq", "value": "ODOO-16"}]},
    )
    assert out["items"][0]["supplier_id"]


# ── describe: both verbs are listed and advertised as committable ────────────────────────────────
def test_describe_lists_both_verbs() -> None:
    client = _client(FakeSystem())
    d = client.get("/nil/v0.1/describe").json()
    assert "procurement.link_supplier" in d["verbs"]
    assert "procurement.unlink_supplier" in d["verbs"]
    rows = {row["verb"]: row for row in d["verb_details"]}
    assert rows["procurement.link_supplier"]["reversibility"] == "REVERSIBLE"
    assert rows["procurement.unlink_supplier"]["reversibility"] == "IRREVERSIBLE"
    assert rows["procurement.link_supplier"]["target"] == "product.supplierinfo"


# ── the compound-dedup edge test (the one permitted edge.py change) ──────────────────────────────
def test_compound_dedup_converges_on_the_ANDed_pair_not_either_field_alone(
    fake_client: _CountingFakeSystem,
) -> None:
    """Two links sharing ONE field (same supplier, different product) must NOT collide — proving the
    dedup probe is a real AND-of-both-fields, not an accidental single-field match."""
    client = _client(fake_client)

    first = _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})
    second = _commit(
        client, "procurement.link_supplier", {"product_ref": "202", "supplier_ref": "7"}
    )  # same supplier, DIFFERENT product — must be a distinct link, not a false convergence

    assert first["result"]["entity"]["id"] != second["result"]["entity"]["id"]
    assert fake_client.created["product.supplierinfo"] == 2

    # re-linking the FIRST pair again must still converge on it alone.
    replay = _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})
    assert replay["result"]["entity"]["id"] == first["result"]["entity"]["id"]
    assert fake_client.created["product.supplierinfo"] == 2  # no third row


def test_compound_dedup_refuses_on_more_than_one_existing_match(fake_client: _CountingFakeSystem) -> None:
    # a pre-existing data anomaly: two supplierinfo rows already link the same pair.
    fake_client.docs["product.supplierinfo"] = [
        {"name": "product.supplierinfo-00001", "product_tmpl_id": 201, "partner_id": 7},
        {"name": "product.supplierinfo-00002", "product_tmpl_id": 201, "partner_id": 7},
    ]
    client = _client(fake_client)

    committed = _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})

    assert committed["state"] == "failed_terminal"
    assert len(fake_client.docs["product.supplierinfo"]) == 2  # no third row minted by a guess


# ── unlink removes the link, and is a convergent no-op the second time ───────────────────────────
def test_unlink_removes_the_link(fake_client: _CountingFakeSystem) -> None:
    client = _client(fake_client)
    linked = _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})
    link_ref = linked["result"]["entity"]["id"]

    unlinked = _commit(client, "procurement.unlink_supplier", {"link_ref": link_ref})

    assert unlinked["state"] == "executed"
    assert fake_client.get("product.supplierinfo", link_ref) is None


def test_unlink_declares_irreversible_honestly() -> None:
    # See translate.PROCUREMENT_UNLINK_SUPPLIER's docstring: edge.py's curated op="delete" branch
    # captures no before-image, so a COMPENSABLE declaration here would be undeliverable at ROLLBACK
    # time — declaring IRREVERSIBLE is the honest choice, not an oversight.
    assert "procurement.unlink_supplier" not in compensation.COMPENSATIONS


# ── C1 (final review): an IRREVERSIBLE delete with no before-image must be HIGH tier, like every
# other delete in this adapter — MEDIUM auto-executes with no human in the loop.
def test_unlink_is_HIGH_tier_declared_and_on_the_wire(fake_client: _CountingFakeSystem) -> None:
    assert translate.PROCUREMENT_UNLINK_SUPPLIER.tier == "HIGH"
    d = _client(fake_client).get("/nil/v0.1/describe").json()
    rows = {row["verb"]: row for row in d["verb_details"]}
    assert rows["procurement.unlink_supplier"]["tier"] == "HIGH"


def test_unlink_proposal_carries_the_HIGH_tier(fake_client: _CountingFakeSystem) -> None:
    client = _client(fake_client)
    linked = _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})
    link_ref = linked["result"]["entity"]["id"]

    proposed = client.post(
        "/nil/v0.1/propose", json=_env("procurement.unlink_supplier", {"link_ref": link_ref})
    ).json()["body"]

    assert proposed["tier"] == "HIGH"


def test_link_rollback_previews_the_unlink(fake_client: _CountingFakeSystem) -> None:
    client = _client(fake_client)
    committed = _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})
    token = committed["compensation"]["token"]

    rolled = client.post("/nil/v0.1/rollback", json={
        "nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": token, "reason": "owner_cancel"},
    }).json()["body"]

    assert rolled["outcome"] == "proposal"
    assert rolled["verb"] == "procurement.unlink_supplier"
    assert rolled["resolved"]["link_ref"] == committed["result"]["entity"]["id"]


# ── nil.search ProductSupplier, unchanged (read side stays as built) ─────────────────────────────
def test_nil_search_product_supplier_exposes_sku_and_supplier_id(fake_client: _CountingFakeSystem) -> None:
    fake_client.docs["product.product"] = [
        {"id": 101, "name": "Widget 16", "default_code": "ODOO-16", "product_tmpl_id": 201},
    ]
    client = _client(fake_client)
    _commit(client, "procurement.link_supplier", {"product_ref": "201", "supplier_ref": "7"})

    out = _query(client, "nil.search", {"target": "ProductSupplier"})

    assert out["items"][0]["sku"] == "ODOO-16"
    assert out["items"][0]["supplier_id"] == "7"


# ── I2 (final review): the default_code lookup must be BATCHED per page, not once per row ─────────
class _CountingSearchFakeSystem(FakeSystem):
    """FakeSystem, plus a log of every `search()` call — proves the batched lookup issues at most one
    `product.product` search per identifier space for a WHOLE page, not one (or two) per row."""

    def __init__(self) -> None:
        super().__init__()
        self.search_calls: list[tuple[str, list]] = []

    def search(self, target: str, domain: list, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append((target, domain))
        return super().search(target, domain, **kwargs)


def test_product_supplier_page_batches_the_default_code_lookup_at_most_two_calls() -> None:
    sys = _CountingSearchFakeSystem()
    page_size = 50
    sys.docs["product.supplierinfo"] = [
        {"id": i, "name": f"product.supplierinfo-{i:05d}", "product_tmpl_id": 1000 + i, "partner_id": 7}
        for i in range(page_size)
    ]
    sys.docs["product.product"] = [
        {"id": 2000 + i, "name": f"Widget {i}", "default_code": f"SKU-{i}", "product_tmpl_id": 1000 + i}
        for i in range(page_size)
    ]
    client = _client(sys)

    out = _query(client, "nil.search", {"target": "ProductSupplier", "limit": page_size})

    assert len(out["items"]) == page_size
    assert all(item.get("sku") == f"SKU-{i}" for i, item in enumerate(out["items"]))
    product_calls = [c for c in sys.search_calls if c[0] == "product.product"]
    assert len(product_calls) <= 2, (
        f"expected at most 2 product.product searches for a {page_size}-row page, saw "
        f"{len(product_calls)}: {product_calls}"
    )
