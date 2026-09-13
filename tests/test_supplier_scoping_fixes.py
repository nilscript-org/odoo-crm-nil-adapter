"""Fix round 1 (review: task-1.1-1.2-review.md) — three Critical + one Important finding on Task 1.2:

C1: `nil.intent {about: "Supplier"}` leaked customers — `IntentResolver` (third-party) has no hook to
    carry a base domain, so it was never scoped like `nil.search` was.
C2: `nil.get {target: "Supplier", id}` returned ANY partner — `ReadPlane.get()` takes no `filter`, so
    resolving the target alone (Task 1.1) never enforced the domain.
C3: the wire `/nil/v0.1/describe`'s `resources` field changed value type (str -> tuple) for every
    resource, breaking `nilscript-controlplane`'s `resource_authority.py` parsers.
I1: `vat` leaked into Customer's default projection because Customer and Supplier shared one
    `_TARGET_FIELDS` entry.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from odoo_nil_adapter import translate
from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.read_plane import OdooReadBackend
from odoo_nil_adapter.system import FakeSystem


def _seeded_partners() -> FakeSystem:
    sys = FakeSystem()
    sys.docs["res.partner"] = [
        {"id": 1, "name": "Pure Customer", "email": "c@x.com", "phone": "111",
         "customer_rank": 1, "supplier_rank": 0, "vat": "VC1"},
        {"id": 2, "name": "Pure Supplier", "email": "s@x.com", "phone": "222",
         "customer_rank": 0, "supplier_rank": 1, "vat": "VS1"},
    ]
    return sys


def _client(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _query(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/query", json=env).json()["data"]


# ── C1: nil.intent must scope Supplier/Customer exactly like nil.search already does ──────────────
def test_intent_about_supplier_returns_only_the_supplier() -> None:
    out = _query(_client(_seeded_partners()), "nil.intent", {"about": "Supplier", "seek": "all"})
    names = [row["name"] for row in out["value"]["items"]]
    assert names == ["Pure Supplier"]


def test_intent_about_customer_returns_only_the_customer() -> None:
    out = _query(_client(_seeded_partners()), "nil.intent", {"about": "Customer", "seek": "all"})
    names = [row["name"] for row in out["value"]["items"]]
    assert names == ["Pure Customer"]


def test_intent_about_supplier_count_excludes_the_customer() -> None:
    out = _query(_client(_seeded_partners()), "nil.intent", {"about": "Supplier", "seek": "count"})
    assert out == {"outcome": "result", "value": {"count": 1}}


# ── C2: nil.get must refuse (the not-found shape) a point read outside the resource's base domain ──
def test_get_supplier_by_a_customers_id_is_not_found() -> None:
    out = _query(_client(_seeded_partners()), "nil.get", {"target": "Supplier", "id": 1})
    assert out == {"found": False, "id": 1}


def test_get_supplier_by_the_suppliers_own_id_returns_the_record() -> None:
    out = _query(_client(_seeded_partners()), "nil.get", {"target": "Supplier", "id": 2})
    assert out.get("id") == 2 and out.get("name") == "Pure Supplier"


def test_get_customer_by_a_suppliers_id_is_not_found() -> None:
    out = _query(_client(_seeded_partners()), "nil.get", {"target": "Customer", "id": 2})
    assert out == {"found": False, "id": 2}


# ── C3: the wire `resources` field must keep its original str-valued shape ────────────────────────
def test_describe_resources_supplier_is_a_plain_model_string() -> None:
    resources = translate.describe()["resources"]
    assert resources["Supplier"] == "res.partner"
    assert all(isinstance(v, str) for v in resources.values())


def test_wire_describe_resources_unchanged_shape_for_every_resource() -> None:
    # Matches what `nilscript-controlplane`'s resource_authority.py expects: every value a plain
    # model-name string, not just the new Supplier entry.
    resources = _client(FakeSystem()).get("/nil/v0.1/describe").json()["resources"]
    assert all(isinstance(v, str) for v in resources.values())
    assert resources["Customer"] == "res.partner"
    assert resources["Supplier"] == "res.partner"


# ── I1: Customer's projection is unchanged; Supplier carries its own (with vat) ────────────────────
def test_customer_default_projection_excludes_vat() -> None:
    schema = OdooReadBackend(FakeSystem()).describe_target("Customer")
    assert "vat" not in schema.default_projection


def test_supplier_default_projection_includes_vat() -> None:
    schema = OdooReadBackend(FakeSystem()).describe_target("Supplier")
    assert "vat" in schema.default_projection


# ── I1 (final review): the projection must be reachable THROUGH THE REAL EDGE, not just at the
# describe_target level — `test_supplier_default_projection_includes_vat` above passed even while the
# real `nil.search`/`nil.get` paths could never reach it (every real read resolved to the native model
# BEFORE the plane was called). These drive the actual wire.
def test_nil_search_supplier_through_the_edge_returns_the_supplier_projection_with_reveal() -> None:
    out = _query(
        _client(_seeded_partners()), "nil.search", {"target": "Supplier", "reveal": ["vat"]}
    )
    assert out["items"] == [
        {"id": 2, "name": "Pure Supplier", "phone": "222", "email": "s@x.com", "vat": "VS1"}
    ]


def test_nil_search_supplier_through_the_edge_redacts_vat_without_reveal() -> None:
    out = _query(_client(_seeded_partners()), "nil.search", {"target": "Supplier"})
    assert "vat" not in out["items"][0]
    assert "vat" in out.get("redacted", [])


def test_nil_search_customer_through_the_edge_never_carries_vat_even_with_reveal() -> None:
    out = _query(
        _client(_seeded_partners()), "nil.search", {"target": "Customer", "reveal": ["vat"]}
    )
    assert all("vat" not in row for row in out["items"])


def test_nil_get_supplier_through_the_edge_returns_vat_with_reveal() -> None:
    out = _query(
        _client(_seeded_partners()), "nil.get", {"target": "Supplier", "id": 2, "reveal": ["vat"]}
    )
    assert out.get("vat") == "VS1"


# ── I3 (final review): the scoped existence count must fail CLOSED, never OPEN ─────────────────────
def test_id_satisfies_domain_propagates_a_refused_scope_count_instead_of_returning_true() -> None:
    """Direct unit proof on the function itself: a scoped count that cannot run must never come back
    as `True` (which would let the caller's own unscoped `get` proceed and possibly hand back a
    record outside the resource's domain)."""

    class _RefusingPlane:
        def count(self, target: str, *, filter: object) -> dict:
            raise translate.CapabilityUnsupported("scoped count refused")

    with pytest.raises(translate.CapabilityUnsupported):
        translate._id_satisfies_domain(_RefusingPlane(), "Supplier", "res.partner", 2)


def test_get_supplier_is_refused_never_found_false_never_the_record_when_the_scope_count_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration proof: once the refusal propagates, `_run_nil_get`'s own try/except turns it into a
    structured `refused` outcome — never the silent `{"found": False}` a normal miss would answer
    (which would misreport "the caller's own read will refuse too" when the refusal is actually caused
    by the scope check's OWN extra predicate), and never the unscoped record."""

    class _BoomPlane:
        def count(self, target: str, *, filter: object) -> dict:
            raise translate.CapabilityUnsupported("scoped count refused")

        def get(self, *args: object, **kwargs: object) -> dict:
            raise AssertionError("must never reach the unscoped get once the scope check refuses")

    monkeypatch.setattr(translate, "_plane", lambda client: _BoomPlane())
    out = translate._run_nil_get(FakeSystem(), {"target": "Supplier", "id": 2})
    assert out == {
        "outcome": "refused",
        "code": "CAPABILITY_UNSUPPORTED",
        "message": "scoped count refused",
    }
