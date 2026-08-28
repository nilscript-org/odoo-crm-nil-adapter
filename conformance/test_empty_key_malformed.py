"""C1.6 — an empty `idempotency_key` is MALFORMED, never a shared business-effect identity.

Every edge read `key = body.get("idempotency_key", "")` and then used `""` like any other key. So
the FIRST keyless COMMIT stored `ledger[""]`, and the SECOND keyless COMMIT of a *different*
proposal answered `replayed: True` **without performing its write** — a receipt for an act that
never ran. Two unrelated business effects shared an identity merely because a caller omitted one.

Not input validation — the **identity model** (BOS plan, locked invariant #5): the business-effect
key is what recovery probes by, so an act with no identity can never be safely retried, and the
honest answer is to refuse *before* the mutation rather than invent a slot. The template
(`nil-adapter-template@288be39`) states the rule; this pins the fork's adherence.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem

WS = "w"


def _app(system: FakeSystem) -> TestClient:
    return TestClient(create_app(system, CapturingEmitter(), bearer=None),
                      raise_server_exceptions=False)


def _propose(client: TestClient, verb: str, args: dict[str, Any]) -> dict[str, Any]:
    env = {"nil": "0.1", "id": "e1", "performative": "PROPOSE", "grant": "g",
           "workspace": WS, "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/propose", json=env).json()["body"]


def _commit(client: TestClient, proposal_id: str, key: str) -> dict[str, Any]:
    env = {"nil": "0.1", "id": "e2", "performative": "COMMIT", "grant": "g",
           "workspace": WS, "body": {"proposal": proposal_id, "idempotency_key": key}}
    return client.post("/nil/v0.1/commit", json=env).json()["body"]


_VERB = "crm.create_lead"
_ARGS = {"name": "ACME Lead"}


def test_a_keyless_commit_is_MALFORMED() -> None:
    client = _app(FakeSystem())
    proposal = _propose(client, _VERB, _ARGS)
    out = _commit(client, proposal["id"], "")
    assert out.get("outcome") == "refusal", out
    assert out["code"] == "MALFORMED" and out.get("field") == "idempotency_key", out


def test_two_keyless_commits_never_share_the_empty_slot() -> None:
    """THE COLLISION: before this, commit #2 of a DIFFERENT proposal replayed commit #1's status
    body — `replayed: True` for a write that never happened."""
    client = _app(FakeSystem())
    first = _commit(client, _propose(client, _VERB, {"name": "One"})["id"], "")
    second = _commit(client, _propose(client, _VERB, {"name": "Two"})["id"], "")
    assert first["code"] == "MALFORMED" and second["code"] == "MALFORMED"
    assert second.get("replayed") is not True, second


def test_a_real_key_still_commits_and_still_replays() -> None:
    """The negative control: the guard fires on ABSENCE only. Widening it would break every
    governed commit in the product."""
    client = _app(FakeSystem())
    proposal = _propose(client, _VERB, _ARGS)
    out = _commit(client, proposal["id"], "k-1")
    assert out.get("state") == "executed", out
    assert _commit(client, proposal["id"], "k-1").get("replayed") is True
