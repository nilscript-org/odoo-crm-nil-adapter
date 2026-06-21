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
