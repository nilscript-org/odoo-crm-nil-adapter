"""D37 (Task 1.2): Odoo can be the system of record for `Supplier` — the same `res.partner` model a
`Customer` read already uses, disambiguated by a FIXED base domain (`supplier_rank > 0` /
`customer_rank > 0`) rather than by guessing which hat the record is wearing.
"""

from __future__ import annotations

from odoo_nil_adapter import translate
from odoo_nil_adapter.read_plane import OdooReadBackend
from odoo_nil_adapter.system import FakeSystem


def test_supplier_reads_only_partners_with_a_supplier_rank(fake_client) -> None:
    translate._run_nil_search(
        fake_client,
        {"target": "Supplier", "filter": [{"field": "email", "op": "eq", "value": "a@b.c"}]},
    )
    assert ("supplier_rank", ">", 0) in fake_client.plane.last_domain
    assert ("email", "=", "a@b.c") in fake_client.plane.last_domain


def test_describe_declares_supplier() -> None:
    assert "Supplier" in translate.describe()["resources"]


def test_customer_keeps_its_own_domain_not_suppliers(fake_client) -> None:
    # The ambiguity is resolved by declaration, not inference: a Customer read must never pick up
    # Supplier's domain (or vice versa) just because they share one model.
    translate._run_nil_search(fake_client, {"target": "Customer", "filter": []})
    assert ("customer_rank", ">", 0) in fake_client.plane.last_domain
    assert not any(triple[0] == "supplier_rank" for triple in fake_client.plane.last_domain)


def test_native_model_name_still_gets_no_base_domain(fake_client) -> None:
    # Decision: a native model name passed directly (not a declared business name) keeps today's
    # behaviour — no base domain is guessed on its behalf.
    translate._run_nil_search(fake_client, {"target": "res.partner", "filter": []})
    assert fake_client.plane.last_target == "res.partner"
    assert fake_client.plane.last_domain == []


def test_describe_target_resolves_supplier_business_name_directly() -> None:
    # Interface note: describe_target("Supplier") must resolve even when called with the business name
    # rather than the already-resolved native model — every current caller resolves first, but
    # describe_target must not silently depend on that staying true forever.
    backend = OdooReadBackend(FakeSystem())
    schema = backend.describe_target("Supplier")
    assert schema is not None
    assert set(schema.default_projection) == {"id", "name", "email", "phone", "vat"}
