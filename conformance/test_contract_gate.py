"""Contract gate (routing plan C2): every CANONICAL verb must turn the baseline capability
contract's inputs into a POSTABLE native payload. The amount-class bug — a canonical arg the
transform silently drops — must fail HERE, not after the owner's approval in production."""

from __future__ import annotations

import pytest

from odoo_nil_adapter.translate import WRITE_VERBS

# The baseline catalog's canonical inputs (nilscript.baseline.catalog) per canonical verb.
CANONICAL_INPUTS: dict[str, dict[str, object]] = {
    "services.create_invoice": {
        "client_id": "9", "currency": "SAR", "amount": "5443", "description": "Graphic design",
    },
    "crm.create_client": {"name": "KO co", "phone": "+9665", "email": "a@b.c"},
    "commerce.record_payment": {"invoice_id": "6", "amount": "100", "method": "cash"},
    "commerce.create_product": {"name": "Design", "price": "50", "sku": "SKU-1"},
    "procurement.create_purchase_invoice": {"supplier_id": "3", "currency": "SAR"},
}


@pytest.mark.parametrize("verb", sorted(CANONICAL_INPUTS))
def test_canonical_verb_declared_and_transforms(verb: str) -> None:
    assert verb in WRITE_VERBS, f"canonical verb {verb!r} is not declared by this adapter"
    wv = WRITE_VERBS[verb]
    inputs = CANONICAL_INPUTS[verb]
    missing = [f for f in wv.required if f not in inputs]
    assert not missing, f"{verb}: adapter requires {missing} which the canonical contract lacks"
    native = wv.to_native(dict(inputs))
    assert isinstance(native, dict) and native, f"{verb}: empty native payload"


def test_invoice_carries_the_priced_line() -> None:
    """The exact live failure (plan B5/B6): a canonical invoice with a flat amount must produce
    a priced line — Odoo rejects posting a line-less customer invoice."""
    native = WRITE_VERBS["services.create_invoice"].to_native(
        dict(CANONICAL_INPUTS["services.create_invoice"])
    )
    lines = native.get("invoice_line_ids")
    assert lines, f"no invoice lines built: {native}"
    _, _, line = lines[0]
    assert line["price_unit"] == 5443.0 and line["name"] == "Graphic design", line


def test_amount_alone_still_builds_a_line() -> None:
    native = WRITE_VERBS["services.create_invoice"].to_native({"client_id": "9", "amount": 100})
    _, _, line = native["invoice_line_ids"][0]
    assert line["price_unit"] == 100.0 and line["name"] == "Service", line
