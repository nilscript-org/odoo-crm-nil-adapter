"""C3.7 — every write verb answers the recovery question, and the answer travels.

The six `a8be065` creates carry an attempt key. The other fifteen carry none — and the plan's
framing (*"the permanent human-gate rows are declared, not forgotten"*) understates what the
inventory actually showed: **a keyless verb is not automatically unsafe.** Twelve of the fifteen
are safe by CONVERGENCE, which is the owner's second shape:

    convergent = a stable pre-existing identity + SET semantics

`sale.confirm_order` on an existing `order_id` reaches the same state however many times it runs —
and the edge already reports `already_confirmed` rather than confirming twice. It needs no attempt
key because replay is a no-op, not because anyone asks. Demanding a key there would add ceremony
without adding safety, and would misreport a safe verb as unsafe.

Three are genuinely `none`, and that is a **correct final answer, not technical debt**:
`crm.create_lead` (no reference field, no dedup key), `crm.log_note` (a `mail.message` nothing
correlates to one attempt), and `commerce.create_product` — whose `default_code` is the SKU, i.e.
**the product's key, not one attempt's**; reusing it as a witness would conflate two identities.

What this file enforces is that the question was ASKED. Silence and a considered "there is nothing
to query on" must never read the same, because only one of them is finished work.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem
from odoo_nil_adapter.translate import WRITE_VERBS

_SHAPES = {"attempt_keyed", "convergent", "none"}


def _rows() -> dict[str, dict]:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None))
    return {r["verb"]: r for r in client.get("/nil/v0.1/describe").json()["verb_details"]}


class TestEveryVerbAnswers:
    def test_no_write_verb_is_silent(self) -> None:
        """THE RATCHET. A new verb arriving with no classification fails here, by name."""
        silent = sorted(
            n for n, v in WRITE_VERBS.items()
            if not v.idempotency_field and not v.recovery_shape
        )
        assert not silent, (
            "these write verbs answer the recovery question with silence — declare a key, "
            "`convergent`, or an honest `none` with its reason",
            silent,
        )

    def test_every_declaration_travels_on_the_wire(self) -> None:
        rows = _rows()
        for name in WRITE_VERBS:
            w = rows[name].get("witness")
            assert w is not None, f"{name} declares a shape the plane cannot read"
            assert w["shape"] in _SHAPES, (name, w)

    def test_a_convergent_verb_names_its_stable_identity(self) -> None:
        """`convergent` without an identity is just an unkeyed write with a nicer label."""
        rows = _rows()
        for name, v in WRITE_VERBS.items():
            if v.recovery_shape == "convergent":
                w = rows[name]["witness"]
                assert w.get("identity"), (name, w)
                assert w.get("lookup"), (name, w)

    def test_an_honest_none_carries_its_REASON(self) -> None:
        """A permanent human-gate has to say why, or nobody can ever tell whether it was
        considered or merely convenient."""
        rows = _rows()
        nones = [n for n, v in WRITE_VERBS.items() if v.recovery_shape == "none"]
        assert nones, "no honest-none verb to prove this on"
        for name in nones:
            assert len(rows[name]["witness"].get("note", "")) > 30, (name, rows[name]["witness"])


class TestTheClassificationIsHONEST:
    def test_create_product_is_NONE_because_the_sku_is_not_an_attempt_key(self) -> None:
        """The one most likely to be mis-declared: `default_code` looks like a witness and is the
        PRODUCT's identity, not one attempt's. Two attempts at creating the same product share it,
        so it cannot distinguish them — which is exactly what a witness must do."""
        v = WRITE_VERBS["commerce.create_product"]
        assert v.recovery_shape == "none"
        assert "SKU" in v.recovery_note or "sku" in v.recovery_note

    def test_the_two_upserts_are_convergent_ONLY_because_a_key_is_now_REQUIRED(self) -> None:
        """Their convergence is not intrinsic — it was bought by C3.5's refusal of a keyless
        upsert. If that guard were removed they would be blind creates again, so the claim and the
        guard must stand or fall together."""
        for name in ("crm.create_client", "crm.create_contact"):
            v = WRITE_VERBS[name]
            assert v.recovery_shape == "convergent" and v.dedup_keys
            assert "C3.5" in v.recovery_note, v.recovery_note

    def test_the_keyed_six_are_untouched(self) -> None:
        rows = _rows()
        keyed = [n for n, v in WRITE_VERBS.items() if v.idempotency_field]
        assert len(keyed) == 6, keyed
        for name in keyed:
            assert rows[name]["witness"]["shape"] == "attempt_keyed"
