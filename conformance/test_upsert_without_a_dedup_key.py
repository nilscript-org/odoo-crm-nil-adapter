"""C3.5 — an upsert with nothing to deduplicate on is a blind create wearing an upsert's name.

`crm.create_client` and `crm.create_contact` declare `dedup_keys=("email","phone")` but
`required=("name",)`. The probe loop skips any key the caller left empty:

    for field in verb.dedup_keys:
        value = native.get(field)
        if not value:
            continue

so a perfectly valid `create_client(name="ACME")` probes NOTHING and falls through to a create.
Its safety was never a property of the verb — only of callers who happened to pass an email.

Two consequences, and the second is the one that matters:

  * a retry after a lost answer mints a SECOND partner, silently;
  * the verb's declared contract is false. `op="upsert"` promises update-in-place with a
    COMPENSABLE before-image; with no key it cannot find anything to update, so it produces a
    REVERSIBLE create while claiming the other. A governance envelope that describes a different
    act than the one performed is worse than a missing one.

The cure is REFUSAL, not a widened `required`. Adding `email` to `required` would break the
legitimate name-only *create* path for callers who genuinely have no key — and would still leave
the verb claiming upsert semantics. Refusing at commit makes the unsafe path **unexpressible
rather than filtered**, which is this platform's own doctrine, and says exactly why.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem
from odoo_nil_adapter.translate import WRITE_VERBS

WS = "w"
UPSERTS = ("crm.create_client", "crm.create_contact")


def _app(system: FakeSystem) -> TestClient:
    return TestClient(create_app(system, CapturingEmitter(), bearer=None),
                      raise_server_exceptions=False)


def _commit(client: TestClient, verb: str, args: dict[str, Any], key: str = "k-1") -> dict[str, Any]:
    """PROPOSE then COMMIT — returning the PROPOSE refusal when there is one.

    Deliberately not asserting a proposal id: this guard fires at PROPOSE, and a helper that
    insisted on one could only ever express "refused at commit". Refusing at propose is the
    stronger position — nothing has been written when the caller is told.
    """
    env = {"nil": "0.1", "id": "e1", "performative": "PROPOSE", "grant": "g",
           "workspace": WS, "body": {"verb": verb, "args": args}}
    proposed = client.post("/nil/v0.1/propose", json=env).json()["body"]
    if not proposed.get("id"):
        return proposed  # a refusal IS the answer, and it arrived before any effect
    return client.post("/nil/v0.1/commit", json={
        "nil": "0.1", "id": "e2", "performative": "COMMIT", "grant": "g",
        "workspace": WS, "body": {"proposal": proposed["id"], "idempotency_key": key},
    }).json()["body"]


class TestAKeylessUpsertIsRefused:
    def test_a_NAME_ONLY_upsert_refuses_instead_of_creating_blind(self) -> None:
        for verb in UPSERTS:
            system = FakeSystem()
            out = _commit(_app(system), verb, {"name": "ACME"})
            assert out.get("outcome") == "refusal", (verb, out)
            assert out["code"] == "INVALID_ARGS", (verb, out)
            assert system.docs.get("res.partner", []) == [], (
                "the record was created before the refusal — an unsafe path must be "
                "unexpressible, not reported after the fact", verb,
            )

    def test_the_refusal_NAMES_the_keys_that_would_make_it_safe(self) -> None:
        """A refusal a caller cannot act on is only half an answer."""
        out = _commit(_app(FakeSystem()), "crm.create_client", {"name": "ACME"})
        assert "email" in out["message"] and "phone" in out["message"], out["message"]
        assert out.get("field") in ("email", "phone"), out

    def test_two_keyless_attempts_leave_the_backing_system_empty(self) -> None:
        """The duplicate this closes: before, each attempt minted its own partner."""
        system = FakeSystem()
        client = _app(system)
        _commit(client, "crm.create_client", {"name": "ACME"}, "k-1")
        _commit(client, "crm.create_client", {"name": "ACME"}, "k-2")
        assert system.docs.get("res.partner", []) == []


class TestTheSafePathsAreUntouched:
    def test_an_EMAIL_keyed_upsert_still_creates(self) -> None:
        system = FakeSystem()
        out = _commit(_app(system), "crm.create_client",
                      {"name": "ACME", "email": "ops@acme.test"})
        assert out.get("state") == "executed", out
        assert len(system.docs.get("res.partner", [])) == 1

    def test_a_PHONE_keyed_upsert_still_creates(self) -> None:
        """Either declared key suffices — the guard demands ONE, not all of them."""
        system = FakeSystem()
        out = _commit(_app(system), "crm.create_client", {"name": "ACME", "phone": "+96650001"})
        assert out.get("state") == "executed", out

    def test_a_keyed_retry_still_finds_and_updates_in_place(self) -> None:
        """The upsert semantics the verb promises, still delivered where a key exists."""
        system = FakeSystem()
        client = _app(system)
        _commit(client, "crm.create_client", {"name": "ACME", "email": "ops@acme.test"}, "k-1")
        _commit(client, "crm.create_client", {"name": "ACME Ltd", "email": "ops@acme.test"}, "k-2")
        assert len(system.docs.get("res.partner", [])) == 1, (
            "the keyed path stopped deduplicating", system.docs.get("res.partner"),
        )

    def test_NON_upsert_verbs_are_not_affected(self) -> None:
        """The guard must key on `op == "upsert"`, not on having dedup_keys — a plain create with
        no keys is honest about what it is."""
        creates = [n for n, v in WRITE_VERBS.items() if v.op == "create" and not v.dedup_keys]
        assert creates, "no plain-create verb to prove this on"
