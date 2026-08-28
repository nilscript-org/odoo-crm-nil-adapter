"""R2 — the witness declaration TRAVELS off this adapter (the matrix's second blocker).

`a8be065` gave six creates a durable recovery witness (`origin`/`ref` stamped `[WSL-<key>]`, probed
ask-first before every keyed create). The witness matrix then found the missing half: it was "SAFE
at the adapter; unreachable from the plane that would recover them" — `describe().verb_details`
carried verb/tier/target/required/reversibility and said nothing about recovery, so no control
plane could ever route a retry by verdict.

This pins the export, in the template's backing-agnostic shape (`nil-adapter-template`,
`recovery.witness_export`): a keyed verb exports `witness{shape, field, lookup,
negative_authoritative}`; an unkeyed verb exports NO witness key at all — silence stays visible,
because the consumer's fail-closed floor (absent ⇒ UNKNOWN ⇒ UNSAFE) only works if absence is
absent.

`negative_authoritative: True` is the Odoo ORM's read-your-writes property: the probe reads the
same committed Postgres store the create writes. The settlement caveat stays where it lives — the
RUNTIME (`edge.py`'s in-doubt handler re-raises on NOT FOUND inside the doubt window, because "Odoo
may still be committing") — the flag describes the query, not the window.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem
from odoo_nil_adapter.translate import WRITE_VERBS

#: The `a8be065` six, by name — pinned so a refactor that quietly drops one from the keyed set
#: fails HERE with the verb's name, not in a production retry.
KEYED = {
    "account.create_invoice": "ref",
    "account.register_payment": "ref",
    "commerce.record_payment": "ref",
    "procurement.create_purchase_invoice": "ref",
    "purchase.create_order": "origin",
    "services.create_invoice": "ref",
}


def _verb_details() -> dict[str, dict]:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None))
    rows = client.get("/nil/v0.1/describe").json()["verb_details"]
    return {r["verb"]: r for r in rows}


class TestTheWitnessTravels:
    def test_every_keyed_verb_exports_its_witness(self) -> None:
        details = _verb_details()
        for verb, field in KEYED.items():
            w = details[verb].get("witness")
            assert w is not None, (
                f"{verb} carries idempotency_field={field!r} at the adapter but exports no "
                "witness — SAFE at the adapter, invisible to the plane, which is the exact "
                "blocker this export exists to close"
            )
            assert w["shape"] == "attempt_keyed"
            assert w["field"] == field
            assert w["negative_authoritative"] is True
            # Q4 must be answered with the probe that actually runs, not a slogan.
            assert "like" in w["lookup"] and "[WSL-" in w["lookup"], w["lookup"]

    def test_the_keyed_set_is_exactly_the_declared_set(self) -> None:
        """Both directions: a verb gaining a field must gain an export; a verb losing one must
        lose it. Drift in either direction is a lie to the plane."""
        declared = {n for n, v in WRITE_VERBS.items() if v.idempotency_field}
        assert declared == set(KEYED), (
            "the adapter's keyed set changed — update this pin AND make sure the recovery "
            "machinery (stamp + ask-first probe) really covers the new member",
            sorted(declared ^ set(KEYED)),
        )

    def test_an_unkeyed_verb_exports_NO_witness_key(self) -> None:
        """Silence travels as silence: `crm.create_lead` has no free-text reference field, and its
        describe row must have NO witness key — not null, not a defaulted shape — so the plane's
        fail-closed floor reads it UNKNOWN and never replays it automatically."""
        details = _verb_details()
        unkeyed = [n for n, v in sorted(WRITE_VERBS.items()) if not v.idempotency_field]
        assert unkeyed, "the adapter should still have unkeyed verbs to prove this on"
        offenders = [n for n in unkeyed if "witness" in details[n]]
        assert not offenders, (
            "an unkeyed verb exported a witness — a plane reading this would replay a verb "
            "whose duplicate nothing can detect",
            offenders,
        )

    def test_the_existing_contract_fields_are_untouched(self) -> None:
        """The export EXTENDS the row; consumers of tier/target/required/reversibility must see
        byte-identical values."""
        row = _verb_details()["purchase.create_order"]
        assert row["tier"] and row["target"] == "purchase.order"
        assert "required" in row and "reversibility" in row
