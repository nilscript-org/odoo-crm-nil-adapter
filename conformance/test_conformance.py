"""Conformance proof for this shim — drives the edge with PROPOSE -> COMMIT per active write verb.

Runs against the in-memory FakeSystem (no live backend). With empty translation stubs every verb
FAILS (the stub raises NotImplementedError) — that is the point: the harness must detect
non-conformance. As you fill `translate.py`, verbs flip to passing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from odoo_crm_nil_adapter.compensation import COMPENSATIONS
from odoo_crm_nil_adapter.edge import CapturingEmitter, create_app
from odoo_crm_nil_adapter.system import FakeSystem
from odoo_crm_nil_adapter.translate import WRITE_VERBS


def _env(verb: str, args: dict) -> dict:
    return {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}


def _commit(client, verb_name: str) -> dict:
    args = {field: "x" for field in WRITE_VERBS[verb_name].required}
    pid = client.post("/nil/v0.1/propose", json=_env(verb_name, args)).json()["body"]["id"]
    return client.post(
        "/nil/v0.1/commit",
        json={"nil": "0.1", "grant": "g", "workspace": "w",
               "body": {"proposal": pid, "idempotency_key": pid}},
    ).json()["body"]


def test_rollback_honesty() -> None:
    """A reversible verb emits a compensation token and ROLLBACK previews (never silently writes);
    an unknown token is refused. Skips only if no verb is mapped reversible in compensation.py."""
    reversible = next((v for v in sorted(WRITE_VERBS) if v in COMPENSATIONS), None)
    if reversible is None:
        pytest.skip("no reversible verb mapped in compensation.py")
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    committed = _commit(client, reversible)
    token = committed.get("compensation", {}).get("token")
    assert token, f"{reversible} is mapped reversible but COMMIT emitted no compensation token"

    rolled = client.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": token, "reason": "owner_cancel"}}).json()["body"]
    assert rolled["outcome"] == "proposal", "ROLLBACK must PREVIEW a compensation, never silently write"

    bogus = client.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": "__no_such_token__", "reason": "owner_cancel"}}).json()["body"]
    assert bogus["outcome"] == "refusal", "an unknown compensation token must be refused, never reversed"


@pytest.mark.parametrize("verb_name", sorted(WRITE_VERBS))
def test_write_verb_reaches_executed(verb_name: str) -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    verb = WRITE_VERBS[verb_name]
    args = {field: "x" for field in verb.required}  # placeholder valid-shaped args

    proposed = client.post("/nil/v0.1/propose", json=_env(verb_name, args)).json()
    proposal_id = proposed.get("body", {}).get("id")
    assert proposal_id, f"{verb_name}: PROPOSE did not yield a proposal: {proposed}"

    committed = client.post(
        "/nil/v0.1/commit",
        json={"nil": "0.1", "grant": "g", "workspace": "w",
               "body": {"proposal": proposal_id, "idempotency_key": proposal_id}},
    )
    state = committed.json().get("body", {}).get("state")
    assert state == "executed", f"{verb_name}: not conformant yet (state={state}) — fill translate.py"


def _seed_contact(client, **fields) -> None:
    """Create a contact through the real write path (propose -> commit), so reads prove read-after-write."""
    pid = client.post("/nil/v0.1/propose", json=_env("crm.create_contact", fields)).json()["body"]["id"]
    client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"proposal": pid, "idempotency_key": pid}})


def _query(client, verb: str, args: dict) -> dict:
    return client.post("/nil/v0.1/query", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"verb": verb, "args": args}}).json()


def test_get_contact_by_phone_returns_matching_contact() -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222")
    _seed_contact(client, name="Omar", phone="0503334444")

    data = _query(client, "crm.get_contact_by_phone", {"phone": "0501112222"})["data"]

    assert data["count"] == 1, f"phone lookup should be exact, got {data}"
    assert data["items"][0]["name"] == "Sara"


def test_get_contact_by_phone_no_match_is_empty_not_error() -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222")

    res = _query(client, "crm.get_contact_by_phone", {"phone": "0000000000"})

    assert "data" in res, "an unmatched phone is a valid empty read, never a 404/refusal"
    assert res["data"]["count"] == 0


def _commit_args(client, verb: str, args: dict) -> dict:
    pid = client.post("/nil/v0.1/propose", json=_env(verb, args)).json()["body"]["id"]
    return client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"proposal": pid, "idempotency_key": pid}}).json()["body"]


def test_update_contact_patches_field_with_read_after_write() -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222", email="old@x.com")

    _commit_args(client, "crm.update_contact", {"contact_id": "Sara", "email": "new@x.com"})

    item = _query(client, "crm.get_contact_by_phone", {"phone": "0501112222"})["data"]["items"][0]
    assert item["email"] == "new@x.com", "update must be visible to a subsequent read"


def test_update_contact_ignores_non_whitelisted_fields() -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222")

    _commit_args(client, "crm.update_contact", {"contact_id": "Sara", "credit_limit": "999999", "name": "Sara K"})

    item = _query(client, "crm.get_contact_by_phone", {"phone": "0501112222"})["data"]["items"][0]
    assert item["name"] == "Sara K", "whitelisted field must be written"
    assert "credit_limit" not in item, "a non-whitelisted field must never reach the backend (mass-assignment guard)"


def test_update_contact_is_compensable_and_rollback_restores_before_image() -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222", email="old@x.com")

    committed = _commit_args(client, "crm.update_contact", {"contact_id": "Sara", "email": "new@x.com"})
    comp = committed.get("compensation", {})
    assert comp.get("reversibility") == "COMPENSABLE", f"a whitelisted contact update must be reversible, got {comp}"
    token = comp.get("token")
    assert token, "COMPENSABLE update must mint a rollback token"

    rolled = client.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": token, "reason": "owner_cancel"}}).json()["body"]
    assert rolled["outcome"] == "proposal", "ROLLBACK previews a restore, never silently writes"
    assert rolled["resolved"]["data"]["email"] == "old@x.com", "rollback must restore the before-image value"


def test_create_contact_upserts_new_identity_reversible_by_delete() -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    committed = _commit_args(client, "crm.create_contact",
        {"name": "Sara", "email": "sara@x.com", "phone": "0501112222"})

    assert committed["compensation"]["reversibility"] == "REVERSIBLE", "a freshly created identity is undone by delete"
    assert _query(client, "crm.get_contact_by_phone", {"phone": "0501112222"})["data"]["count"] == 1


def test_create_contact_upsert_dedups_on_email_instead_of_duplicating() -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _commit_args(client, "crm.create_contact", {"name": "Sara", "email": "sara@x.com", "phone": "0501112222"})

    _commit_args(client, "crm.create_contact", {"name": "Sara K", "email": "sara@x.com", "phone": "0509999999"})

    rows = _query(client, "crm.list_contacts", {"match": {"email": "sara@x.com"}})["data"]
    assert rows["count"] == 1, "at-least-once webhook retry must update the identity, never duplicate it"
    assert rows["items"][0]["name"] == "Sara K", "the matched record is updated in place"


def test_create_contact_upsert_matched_update_is_compensable() -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _commit_args(client, "crm.create_contact", {"name": "Sara", "email": "sara@x.com", "phone": "0501112222"})

    committed = _commit_args(client, "crm.create_contact", {"name": "Sara K", "email": "sara@x.com"})

    comp = committed["compensation"]
    assert comp["reversibility"] == "COMPENSABLE", "an upsert that MATCHED is reversed by restoring the before-image"
    rolled = client.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": comp["token"], "reason": "owner_cancel"}}).json()["body"]
    assert rolled["outcome"] == "proposal"
    assert rolled["resolved"]["data"]["name"] == "Sara", "rollback restores the pre-upsert name"


def test_log_note_posts_to_chatter_and_is_irreversible() -> None:
    sys = FakeSystem()
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222")

    committed = _commit_args(client, "crm.log_note", {"contact_id": "Sara", "body": "called the customer"})

    assert committed["state"] == "executed"
    comp = committed["compensation"]
    assert comp["reversibility"] == "IRREVERSIBLE", "append-only chatter is honestly irreversible"
    assert "token" not in comp, "an IRREVERSIBLE effect must not mint a rollback token"
    assert sys.messages[("res.partner", "Sara")] == ["called the customer"], "the note must reach the record"


def test_log_note_rollback_is_refused() -> None:
    sys = FakeSystem()
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222")
    _commit_args(client, "crm.log_note", {"contact_id": "Sara", "body": "note"})

    # there is no token to roll back; any attempt is refused, never a fabricated reversal
    refused = client.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": "anything", "reason": "x"}}).json()["body"]
    assert refused["outcome"] == "refusal"


def test_create_contact_upsert_refuses_ambiguous_match() -> None:
    # Two existing records share an email (e.g. a shared company inbox). An upsert keyed on that
    # email is AMBIGUOUS: silently updating one (or creating a third) corrupts the identity graph.
    sys = FakeSystem()
    sys.create("res.partner", {"name": "A", "email": "team@acme.com"})
    sys.create("res.partner", {"name": "B", "email": "team@acme.com"})
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    committed = _commit_args(client, "crm.create_contact", {"name": "C", "email": "team@acme.com"})

    assert committed["state"] == "failed_terminal", f"ambiguous upsert must fail, not guess: {committed}"
    assert len(sys.docs["res.partner"]) == 2, "an ambiguous upsert must never create a third duplicate"


def test_describe_exposes_skeleton() -> None:
    """MANDATORY: /nil/v0.1/describe exposes a valid skeleton — nil version, a verb catalog, and
    per native target {exists, fields}. This is the universal connect handshake the kernel uses."""
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    d = client.get("/nil/v0.1/describe").json()
    assert d.get("nil") == "0.1", "describe must report the NIL version"
    assert d.get("verbs"), "describe must list the verb catalog"
    targets = d.get("targets", {})
    assert isinstance(targets, dict) and targets, "describe must report native targets"
    for name, t in targets.items():
        assert isinstance(t, dict) and "exists" in t and "fields" in t, f"{name}: target needs exists+fields"
    assert all(t["exists"] for t in targets.values()), "FakeSystem targets are always provisioned"
