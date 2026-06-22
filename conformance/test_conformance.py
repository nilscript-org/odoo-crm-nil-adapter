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


class _DropsEmailBackend(FakeSystem):
    """Mimics a backend that silently ignores a field on write (Odoo dropping an unresolved value)."""

    def update(self, target, record_id, doc):
        return super().update(target, record_id, {k: v for k, v in doc.items() if k != "email"})


def test_verified_is_false_when_a_written_field_does_not_persist() -> None:
    # The core of the thesis: the success envelope must be EARNED by a read-back, never asserted.
    sys = _DropsEmailBackend()
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222")

    committed = _commit_args(client, "crm.update_contact", {"contact_id": "Sara", "email": "new@x.com"})

    result = committed["result"]
    assert result["verified"] is False, "a field that did not land in the SSOT must NOT report verified"
    assert "email" in result.get("unverified_fields", []), "the unverified field must be named"
    assert result["claim"] == "partial", "an unverified write is partial success, not success"


def test_verified_is_true_when_the_write_actually_persists() -> None:
    # Guard against crying wolf: a write that genuinely lands must still verify true.
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222")

    committed = _commit_args(client, "crm.update_contact", {"contact_id": "Sara", "email": "new@x.com"})

    assert committed["result"]["verified"] is True
    assert committed["result"].get("unverified_fields", []) == []


def test_resource_update_is_also_honestly_verified() -> None:
    # The generic resource.* path must earn verified too — not just the curated crm.* verbs.
    sys = _DropsEmailBackend()
    sys.create("res.partner", {"name": "Sara"})
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    pid = client.post("/nil/v0.1/propose", json=_env("resource.update",
        {"target": "res.partner", "id": "Sara", "data": {"email": "new@x.com"}})).json()["body"]["id"]
    committed = client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"proposal": pid, "idempotency_key": pid}}).json()["body"]

    assert committed["result"]["verified"] is False, "resource.* must not assert verified either"
    assert "email" in committed["result"].get("unverified_fields", [])


def test_executed_event_carries_per_field_before_after_diff() -> None:
    # The control-plane field table needs more than a named drop: for each written field it shows
    # what it WAS, what was ASKED, and what actually LANDED. A dropped field must read after≠requested
    # so the silent drop is visible field-by-field, not just listed in unverified_fields.
    sys = _DropsEmailBackend()
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222")

    committed = _commit_args(client, "crm.update_contact", {"contact_id": "Sara", "email": "new@x.com"})

    fields = committed["result"]["ssot"].get("fields", [])
    by = {f["field"]: f for f in fields}
    assert "email" in by, "every written field must appear in the per-field diff"
    email = by["email"]
    assert email["requested"] == "new@x.com"
    assert email["verified"] is False                 # it did not persist
    assert email["after"] != "new@x.com"              # the SSOT shows the real (dropped) value, not the ask
    assert "before" in email                          # the prior value is captured for before→after


def test_field_diff_shows_after_equals_requested_on_a_clean_write() -> None:
    # Guard against crying wolf: a field that genuinely lands must read verified with after==requested.
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Sara", phone="0501112222")

    committed = _commit_args(client, "crm.update_contact", {"contact_id": "Sara", "email": "new@x.com"})

    email = {f["field"]: f for f in committed["result"]["ssot"]["fields"]}["email"]
    assert email["verified"] is True and email["after"] == "new@x.com"


def test_choice_gate_refuses_unresolvable_country_with_candidates() -> None:
    # The Choice Gate: a constrained value that doesn't resolve is REFUSED at PROPOSE with the live
    # candidate list — so the agent picks the real member (e.g. "قطر", whose Arabic name doesn't match
    # Odoo's English res.country, gets the list and chooses Qatar) instead of a silent/wrong write.
    sys = FakeSystem()
    sys.docs["res.country"] = [{"id": 186, "name": "Qatar", "code": "QA"},
                               {"id": 184, "name": "Saudi Arabia", "code": "SA"}]
    sys.schemas["res.partner"] = [{"name": "country_id", "type": "many2one", "relation": "res.country"}]
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    proposed = client.post("/nil/v0.1/propose", json=_env("crm.update_contact",
        {"contact_id": "37", "country": "قطر"})).json()["body"]

    assert proposed["outcome"] == "refusal"          # not silently accepted
    assert proposed["field"] == "country_id"
    assert proposed.get("candidates"), "the refusal must carry the live options to choose from"
    names = {c.get("name") for c in proposed["candidates"]}
    assert "Qatar" in names                          # the agent now sees the real member to pick


def test_choice_gate_passes_a_resolvable_value() -> None:
    # A value that DOES resolve sails through the gate — proposal, not refusal.
    sys = FakeSystem()
    sys.docs["res.country"] = [{"id": 186, "name": "Qatar", "code": "QA"}]
    sys.schemas["res.partner"] = [{"name": "country_id", "type": "many2one", "relation": "res.country"}]
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    proposed = client.post("/nil/v0.1/propose", json=_env("crm.update_contact",
        {"contact_id": "37", "country": "Qatar"})).json()["body"]

    assert proposed["outcome"] == "proposal"         # "Qatar" resolves → no gate refusal


def test_update_contact_resolves_country_dropdown_to_reference_id() -> None:
    # country_id is a many2one (dropdown → res.country). The adapter must resolve the human value to
    # the backend record id; writing the raw text would be rejected by the relational field. This is
    # the whole point — the agent says "Qatar", the adapter writes the id the field actually accepts.
    sys = FakeSystem()
    sys.docs["res.country"] = [{"id": 190, "name": "Qatar", "code": "QA"},
                               {"id": 184, "name": "Saudi Arabia", "code": "SA"}]
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Badr", phone="0509998888")

    committed = _commit_args(client, "crm.update_contact", {"contact_id": "Badr", "country": "Qatar"})

    assert sys.get("res.partner", "Badr")["country_id"] == 190           # resolved id, not the text
    f = {x["field"]: x for x in committed["result"]["ssot"]["fields"]}
    assert f["country_id"]["requested"] == 190 and f["country_id"]["after"] == 190
    assert f["country_id"]["verified"] is True and committed["result"]["verified"] is True


def test_update_contact_resolves_country_by_iso_code() -> None:
    sys = FakeSystem()
    sys.docs["res.country"] = [{"id": 190, "name": "Qatar", "code": "QA"}]
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Badr", phone="0507776666")

    committed = _commit_args(client, "crm.update_contact", {"contact_id": "Badr", "country": "QA"})

    assert committed["result"]["verified"] is True and sys.get("res.partner", "Badr")["country_id"] == 190


def test_update_contact_terminal_fails_on_unresolvable_country() -> None:
    # No silent write: a country that matches nothing fails terminally, never writes a bad value.
    sys = FakeSystem()
    sys.docs["res.country"] = [{"id": 190, "name": "Qatar", "code": "QA"}]
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    _seed_contact(client, name="Badr", phone="0501112233")

    committed = _commit_args(client, "crm.update_contact", {"contact_id": "Badr", "country": "Atlantis"})

    assert committed["state"] == "failed_terminal"
    assert "country_id" not in (sys.get("res.partner", "Badr") or {})   # nothing written


def test_list_countries_reads_the_dropdown_values() -> None:
    # The read surface behind the resolver — closes the "no way to see country values" gap.
    sys = FakeSystem()
    sys.docs["res.country"] = [{"id": 190, "name": "Qatar", "code": "QA"}]
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    resp = client.post("/nil/v0.1/query", json=_env("crm.list_countries", {})).json()

    assert resp["data"]["target"] == "res.country"
    assert resp["data"]["items"][0]["name"] == "Qatar"


def test_resource_update_resolves_selection_and_reference_from_schema() -> None:
    # P2 — schema-driven resolution: a selection value resolves to its stored key (bucket B, the
    # "متاح" case), a many2one value to the referenced id (bucket C) — automatically, no per-field
    # declaration. Driven purely by the field metadata schema() exposes.
    sys = FakeSystem()
    sys.docs["res.country"] = [{"id": 190, "name": "Qatar", "code": "QA"}]
    sys.schemas["res.partner"] = [
        {"name": "state", "type": "selection",
         "options": [{"value": "available", "label": "Available"}, {"value": "sold", "label": "Sold"}]},
        {"name": "country_id", "type": "many2one", "relation": "res.country"},
    ]
    sys.create("res.partner", {"name": "Badr"})
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    pid = client.post("/nil/v0.1/propose", json=_env("resource.update", {"target": "res.partner",
        "id": "Badr", "data": {"state": "Available", "country_id": "Qatar"}})).json()["body"]["id"]
    committed = client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"proposal": pid, "idempotency_key": pid}}).json()["body"]

    rec = sys.get("res.partner", "Badr")
    assert rec["state"] == "available"      # selection label → stored key (B)
    assert rec["country_id"] == 190         # many2one value → referenced id (C), undeclared
    assert committed["result"]["verified"] is True


def test_resource_update_resolves_multi_value_tags() -> None:
    # P3 (D) — a multi-value field (many2many/tags) resolves element-by-element to referenced ids,
    # preserving cardinality. "VIP", "Wholesale" → [1, 2].
    sys = FakeSystem()
    sys.docs["res.partner.category"] = [{"id": 1, "name": "VIP"}, {"id": 2, "name": "Wholesale"}]
    sys.schemas["res.partner"] = [
        {"name": "category_id", "type": "many2many", "relation": "res.partner.category"},
    ]
    sys.create("res.partner", {"name": "Badr"})
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    pid = client.post("/nil/v0.1/propose", json=_env("resource.update", {"target": "res.partner",
        "id": "Badr", "data": {"category_id": ["VIP", "Wholesale"]}})).json()["body"]["id"]
    committed = client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"proposal": pid, "idempotency_key": pid}}).json()["body"]

    assert sys.get("res.partner", "Badr")["category_id"] == [1, 2]   # each tag → its id
    assert committed["result"]["verified"] is True


def test_resource_update_refuses_unknown_selection_value_at_propose() -> None:
    # Fail-closed for bucket B, now enforced earlier by the Choice Gate: a value outside the field's
    # allowed set is REFUSED at PROPOSE with the allowed options as candidates — never reaches a write.
    sys = FakeSystem()
    sys.schemas["thing"] = [{"name": "state", "type": "selection",
                             "options": [{"value": "available", "label": "Available"}]}]
    sys.create("thing", {"name": "x"})
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    proposed = client.post("/nil/v0.1/propose", json=_env("resource.update",
        {"target": "thing", "id": "x", "data": {"state": "NoSuchStatus"}})).json()["body"]

    assert proposed["outcome"] == "refusal" and proposed["field"] == "state"
    assert {c["value"] for c in proposed["candidates"]} == {"available"}  # the allowed set, to pick from
    assert "state" not in (sys.get("thing", "x") or {})  # nothing written


def test_schema_extracts_selection_options_relation_and_readonly() -> None:
    # P1 — the resolver needs to know HOW to write each field. schema() must surface a selection's
    # option list (enum), a many2one's relation (comodel), and the readonly flag — from fields_get.
    from odoo_crm_nil_adapter.system import RealSystemClient

    c = RealSystemClient("http://x", db="d", login="l", api_key="k")
    fake_fields_get = {
        "name": {"type": "char", "required": True, "readonly": False},
        "country_id": {"type": "many2one", "relation": "res.country", "readonly": False},
        "state": {"type": "selection", "selection": [["available", "Available"], ["sold", "Sold"]]},
        "create_date": {"type": "datetime", "readonly": True},
    }
    c._kw = lambda *a, **k: fake_fields_get  # type: ignore[assignment]  # stub the XML-RPC call

    fields = {f["name"]: f for f in c.schema("res.partner")}
    assert fields["country_id"]["relation"] == "res.country"            # C: where it points
    assert fields["state"]["options"] == [{"value": "available", "label": "Available"},
                                          {"value": "sold", "label": "Sold"}]  # B: the allowed list
    assert fields["create_date"]["readonly"] is True                    # F: never a write target
    assert "options" not in fields["name"] and "relation" not in fields["name"]  # A: plain scalar


def test_describe_surfaces_field_metadata_for_callers() -> None:
    # The read surface behind resolution: a caller (agent/UI) can see a field's allowed values and
    # where a reference points — so "what can this field be?" is answerable before a write.
    sys = FakeSystem()
    sys.schemas["res.partner"] = [
        {"name": "name", "type": "char", "required": True, "readonly": False},
        {"name": "country_id", "type": "many2one", "required": False, "readonly": False,
         "relation": "res.country"},
        {"name": "state", "type": "selection", "required": False, "readonly": False,
         "options": [{"value": "available", "label": "Available"}, {"value": "sold", "label": "Sold"}]},
    ]
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    d = client.get("/nil/v0.1/describe").json()
    fields = {f["name"]: f for f in d["targets"]["res.partner"]["fields"]}
    assert fields["country_id"]["relation"] == "res.country"
    assert {o["value"] for o in fields["state"]["options"]} == {"available", "sold"}


def test_propose_flags_unsupported_args_as_ignored() -> None:
    # An arg the verb cannot write must be surfaced as ignored, never echoed as accepted.
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    proposed = client.post("/nil/v0.1/propose", json=_env("crm.update_contact",
        {"contact_id": "10", "credit_limit": 5000, "country": "السعودية", "email": "x@y.com"})).json()["body"]

    assert "credit_limit" in proposed.get("ignored", []), "an unwritable arg must be flagged, not silently dropped"
    assert "email" not in proposed.get("ignored", []), "a supported arg must not be flagged"
    assert "country" not in proposed.get("ignored", []), "country is now a supported (resolved) arg, not ignored"


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
