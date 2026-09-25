"""T3/O9: status is answered by the commit's own attempt key, read from Odoo — never from the
adapter's in-process memory (`state.ledger`/`state.executed`), which a restart erases. Quotes "The
status-by-key contract" (plan 2026-09-25-simplicity-w0-defects.md, shared by T2/T3/T4/T5/T6):

    GET /nil/v0.1/status/{proposal_id}?verb=<verb>&idempotency_key=<key>
    STATUS: {"state": "executed"|"not_found"|"unknown", "authoritative": <bool>,
             "result": {"external_ref": <str>|None}, "reason": <str>}

`executed`: Odoo holds a record carrying the key. `not_found` + `authoritative: true`: the SAME
ask-first probe the commit path already runs finds nothing — an exact key filter the backend
honours, so a re-commit under the identical key is safe. Anything else (ambiguous, unreachable, a
verb with nowhere to put a key) is `unknown` — no answer yet, never a guess.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem


def _po_system() -> FakeSystem:
    sys = FakeSystem()
    # `origin` is a real, writable Char on purchase.order (as `_stampable` would find via fields_get).
    sys.schemas["purchase.order"] = [{"name": "origin", "type": "char", "readonly": False}]
    return sys


def _like_to_regex(pattern: str) -> str:
    """A minimal SQL LIKE → regex translator (honours `\\` escapes of `\\`, `%`, `_`) — real Odoo
    forwards a `like` domain straight to Postgres, where a bare `_` matches ANY single character and
    `%` matches any run. The built-in `FakeSystem.search`'s `like` is a plain Python substring check
    and cannot exercise that wildcard-collision, so this test double reproduces the real semantics."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == "%":
            out.append(".*")
        elif c == "_":
            out.append(".")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class _SQLLikeSystem(FakeSystem):
    """A fake whose `search` honours REAL SQL LIKE wildcard semantics for the `like`/`ilike` ops used
    by the ask-first marker probe — everything else is the inherited in-memory behaviour."""

    def search(self, target: str, domain: list[list[Any]], *, fields: Any = None,
               limit: int = 50, order: str | None = None) -> list[dict[str, Any]]:
        field, op, value = domain[0]
        assert op in ("like", "ilike"), f"unexpected op in this test double: {op}"
        rx = _like_to_regex(str(value))
        flags = re.IGNORECASE if op == "ilike" else 0
        rows = [r for r in self.docs.get(target, [])
                if re.search(rx, str(r.get(field, "")), flags)]
        return rows[:limit]


def _product_system() -> FakeSystem:
    sys = FakeSystem()
    sys.schemas["product.product"] = [{"name": "description", "type": "text", "readonly": False}]
    return sys


def _app(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _commit(client: TestClient, verb: str, args: dict[str, Any], key: str) -> dict[str, Any]:
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}
    pid = client.post("/nil/v0.1/propose", json=env).json()["body"]["id"]
    return client.post(
        "/nil/v0.1/commit",
        json={"nil": "0.1", "grant": "g", "workspace": "w",
              "body": {"proposal": pid, "idempotency_key": key}},
    ).json()["body"]


def _status(client: TestClient, proposal_id: str, verb: str, key: str) -> dict[str, Any]:
    resp = client.get(f"/nil/v0.1/status/{proposal_id}",
                       params={"verb": verb, "idempotency_key": key})
    return resp.json()["body"]


def test_executed_is_read_from_odoo_by_key_after_a_restart() -> None:
    """THE point: a FRESH app (the adapter restarted; its in-process ledger remembers nothing) still
    answers `executed`, because the truth lives in Odoo, in the field the commit stamped."""
    sys = _po_system()
    committed = _commit(_app(sys), "purchase.create_order", {"partner_id": "7"}, "prep:abc:r1")
    assert committed["state"] == "executed"
    fresh = _app(sys)  # a NEW adapter process over the SAME backing system
    body = _status(fresh, "some-other-proposal-id", "purchase.create_order", "prep:abc:r1")
    assert body["state"] == "executed"
    assert body["authoritative"] is True
    assert body["result"]["external_ref"]


def test_not_found_is_authoritative_when_nothing_carries_the_key() -> None:
    """An empty search over an exact key filter is safe to commit again under the SAME key."""
    sys = _po_system()
    body = _status(_app(sys), "pid", "purchase.create_order", "prep:never-happened:r1")
    assert body["state"] == "not_found"
    assert body["authoritative"] is True


def test_two_matching_records_answer_unknown_never_a_guess() -> None:
    sys = _po_system()
    marker = "[WSL-prep-dup-r1]"
    sys.docs["purchase.order"] = [
        {"id": 1, "name": "P00001", "origin": marker, "target": "purchase.order"},
        {"id": 2, "name": "P00002", "origin": marker, "target": "purchase.order"},
    ]
    body = _status(_app(sys), "pid", "purchase.create_order", "prep-dup-r1")
    assert body["state"] == "unknown"
    assert body["authoritative"] is False


def test_convergent_verb_reports_not_found_authoritative_replay_is_safe() -> None:
    """`sale.confirm_order` is SET semantics over a pre-existing id — no probe needed at all."""
    sys = FakeSystem()
    body = _status(_app(sys), "pid", "sale.confirm_order", "prep:xyz:r1")
    assert body["state"] == "not_found"
    assert body["authoritative"] is True
    assert "convergent" in body["reason"].lower()


def test_a_verb_with_no_queryable_identity_is_unknown() -> None:
    """`crm.create_lead` has nowhere to put an attempt key — the honest answer is `unknown`."""
    sys = FakeSystem()
    body = _status(_app(sys), "pid", "crm.create_lead", "prep:abc:r1")
    assert body["state"] == "unknown"
    assert body["authoritative"] is False


def test_a_call_with_no_key_or_verb_falls_back_to_the_legacy_memory_answer() -> None:
    """Backward compatible: the old shape stands when the caller does not opt into status-by-key."""
    sys = _po_system()
    app = _app(sys)
    env = {"nil": "0.1", "grant": "g", "workspace": "w",
           "body": {"verb": "purchase.create_order", "args": {"partner_id": "7"}}}
    pid = app.post("/nil/v0.1/propose", json=env).json()["body"]["id"]
    app.post("/nil/v0.1/commit",
              json={"nil": "0.1", "grant": "g", "workspace": "w",
                    "body": {"proposal": pid, "idempotency_key": "prep:abc:r1"}})
    body = app.get(f"/nil/v0.1/status/{pid}").json()["body"]
    assert body["state"] == "executed"
    assert "authoritative" not in body


def test_create_product_is_now_keyed_and_status_reads_it_back_after_a_restart() -> None:
    """T3/O9: `commerce.create_product` becomes attempt_keyed via `description` (never `default_code`,
    which is the product's own identity, not one attempt's)."""
    sys = _product_system()
    committed = _commit(_app(sys), "commerce.create_product", {"name": "Widget"}, "prep:prod:r1")
    assert committed["state"] == "executed"
    assert len(sys.docs["product.product"]) == 1
    fresh = _app(sys)
    body = _status(fresh, "pid", "commerce.create_product", "prep:prod:r1")
    assert body["state"] == "executed"
    assert body["authoritative"] is True


def test_a_second_create_product_commit_with_the_same_key_creates_nothing_new() -> None:
    sys = _product_system()
    app = _app(sys)
    first = _commit(app, "commerce.create_product", {"name": "Widget"}, "prep:prod:r1")
    assert first["state"] == "executed"
    second = _commit(_app(sys), "commerce.create_product", {"name": "Widget"}, "prep:prod:r1")
    assert second["state"] == "executed"
    assert second.get("replayed") is True
    assert len(sys.docs["product.product"]) == 1, "a second product was created"


def test_a_literal_underscore_in_the_key_does_not_wildcard_match_a_different_record() -> None:
    """Real SQL LIKE treats a bare `_` as "match any one character". An attempt key that happens to
    contain an underscore must not be able to wildcard-match a DIFFERENT attempt's marker that
    merely carries some other character in that same position — that would be a false `executed`
    against a record that has nothing to do with this attempt, reported as an AUTHORITATIVE answer."""
    sys = _SQLLikeSystem()
    sys.schemas["purchase.order"] = [{"name": "origin", "type": "char", "readonly": False}]
    # a genuinely different attempt's marker — differs from ours ONLY at the `_`/`X` position
    other = "[WSL-prep:abcXr1]"
    sys.docs["purchase.order"] = [
        {"id": 1, "name": "P00001", "origin": other, "target": "purchase.order"},
    ]
    body = _status(_app(sys), "pid", "purchase.create_order", "prep:abc_r1")
    assert body["state"] == "not_found", body  # must NOT report the other attempt's record as ours
    assert body["authoritative"] is True
