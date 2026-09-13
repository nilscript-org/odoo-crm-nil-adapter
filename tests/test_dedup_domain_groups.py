"""I4 fix round 2 (re-review): `edge.py`'s generic upsert-dedup mechanism gained a second compound
shape — a group of fully-resolved domain TRIPLES (`(field, op, value)`), ANDed verbatim, alongside
the original compound shape (a tuple of plain field names, probed by equality against `native`). The
two are told apart by `_is_domain_group`, which inspects TUPLE SHAPE only — never a business field
name, so the mechanism stays vendor-neutral exactly like the round-1 compound-key generalization it
extends.

This pins that shape-detection contract directly, independent of any one verb, so every OTHER verb's
dedup — `crm.create_contact`/`crm.create_client`'s `("email", "phone")`,
`procurement.link_supplier`'s `(("product_tmpl_id", "partner_id"),)` — keeps taking the SAME
equality-against-`native` branch it always has, byte-for-byte.
"""

from __future__ import annotations

from odoo_nil_adapter.edge import _is_domain_group


def test_a_plain_field_name_is_not_a_domain_group() -> None:
    assert _is_domain_group("email") is False


def test_a_compound_tuple_of_field_names_is_not_a_domain_group() -> None:
    # crm.create_contact / crm.create_client's declared dedup_keys entries.
    assert _is_domain_group(("email", "phone")) is False
    # procurement.link_supplier's compound key (fix round 1).
    assert _is_domain_group(("product_tmpl_id", "partner_id")) is False


def test_a_compound_group_of_domain_triples_is_a_domain_group() -> None:
    # procurement.create_supplier's new shape (fix round 2): a real RANGE predicate, which the
    # field-name shape above can never express (it is equality-only against `native`).
    assert _is_domain_group((("email", "=", "a@b.c"), ("supplier_rank", ">", 0))) is True
    assert _is_domain_group((("name", "=", "Acme"), ("supplier_rank", ">", 0))) is True


def test_an_empty_tuple_is_not_a_domain_group() -> None:
    assert _is_domain_group(()) is False


def test_a_single_domain_triple_group_of_one_is_still_a_domain_group() -> None:
    assert _is_domain_group((("supplier_rank", ">", 0),)) is True
