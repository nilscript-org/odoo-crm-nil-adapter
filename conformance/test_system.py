"""Unit tests for the FakeSystem I/O double — keeps it in behavioural lockstep with RealSystemClient.

These pin the spine primitives (`search`, `message_post`, `call_method`) the semantic verbs build on.
RealSystemClient can't be unit-tested without a live Odoo, so parity is held by matching contracts:
the domain shape FakeSystem interprets here is the exact `search_read` domain RealSystemClient forwards.
"""

from __future__ import annotations

from odoo_crm_nil_adapter.system import FakeSystem, SystemError
from odoo_crm_nil_adapter.translate import _run_get_contact_by_phone


class _PickyFake(FakeSystem):
    """Mimics live Odoo: a search on a field the model doesn't have raises (here, anything but phone)."""

    def search(self, target, domain, *, fields=None, limit=50):
        for field, _op, _value in domain:
            if field != "phone":
                raise SystemError(f"Invalid field {field!r} on model {target}")
        return super().search(target, domain, fields=fields, limit=limit)


def test_get_contact_by_phone_survives_unknown_fallback_field() -> None:
    # A number that doesn't exact-match `phone` triggers the secondary lookup. On real Odoo that
    # field must exist; if a backend rejects it, the READ degrades to empty — never a 500.
    sys = _PickyFake()
    sys.create("res.partner", {"name": "Sara", "phone": "0501112222"})

    out = _run_get_contact_by_phone(sys, {"phone": "0000000000"})

    assert out["count"] == 0


def test_search_exact_match_returns_only_that_record() -> None:
    sys = FakeSystem()
    sys.create("res.partner", {"name": "Sara", "phone": "0501112222"})
    sys.create("res.partner", {"name": "Omar", "phone": "0503334444"})

    rows = sys.search("res.partner", [["phone", "=", "0501112222"]])

    assert [r["name"] for r in rows] == ["Sara"]


def test_search_no_match_returns_empty() -> None:
    sys = FakeSystem()
    sys.create("res.partner", {"name": "Sara", "phone": "0501112222"})

    assert sys.search("res.partner", [["phone", "=", "9999"]]) == []


def test_search_ilike_is_case_insensitive_substring() -> None:
    sys = FakeSystem()
    sys.create("res.partner", {"name": "Sara Ali"})

    assert len(sys.search("res.partner", [["name", "ilike", "ALI"]])) == 1


def test_message_post_appends_to_record_chatter() -> None:
    sys = FakeSystem()
    sys.create("res.partner", {"name": "Sara"})

    sys.message_post("res.partner", "Sara", "called the customer")
    sys.message_post("res.partner", "Sara", "follow-up next week")

    assert sys.messages[("res.partner", "Sara")] == ["called the customer", "follow-up next week"]


def test_search_anding_multiple_triples() -> None:
    sys = FakeSystem()
    sys.create("res.partner", {"name": "Sara", "phone": "0501112222", "company_name": "Acme"})
    sys.create("res.partner", {"name": "Sara", "phone": "0509999999", "company_name": "Globex"})

    rows = sys.search("res.partner", [["name", "=", "Sara"], ["company_name", "=", "Acme"]])

    assert [r["phone"] for r in rows] == ["0501112222"]
