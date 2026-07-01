"""Reference legibility (docs/reference-legibility.md in the nilscript repo) — replays the exact
"country_id = 224" incident and proves both faults are closed:

  Fault A — a bare foreign key must be approved by resolved NAME, not a magic number.
  Fault B — the read-back receipt must re-resolve the LANDED value to its label, so the agent
            cannot narrate 224 as "Uzbekistan"; the Observation is built from live data.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem


def _seeded() -> FakeSystem:
    sys = FakeSystem()
    sys.schemas["res.partner"] = [
        {"name": "name", "type": "char"},
        {"name": "country_id", "type": "many2one", "relation": "res.country"},
    ]
    # The incident's live country table: 192 is Saudi Arabia, 224 is Türkiye (NOT Uzbekistan).
    sys.docs["res.country"] = [
        {"name": "Saudi Arabia", "id": 192, "code": "SA", "target": "res.country"},
        {"name": "Türkiye", "id": 224, "code": "TR", "target": "res.country"},
    ]
    sys.docs["res.partner"] = [{"name": "43", "id": 43, "country_id": False, "target": "res.partner"}]
    return sys


def _client() -> TestClient:
    return TestClient(create_app(_seeded(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _propose(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/propose", json=env).json()["body"]


def test_proposal_echoes_country_by_name_not_bare_id() -> None:
    """Fault A: the agent sets country_id to the bare integer 224; the proposal reveals it as
    Türkiye in both `resolved.references` and the preview text, so the wrong pick is visible."""
    body = _propose(_client(), "resource.update",
                    {"target": "res.partner", "id": "43", "data": {"country_id": 224}})
    assert body["outcome"] == "proposal"
    assert body["resolved"]["references"]["country_id"] == {"value": 224, "label": "Türkiye (TR)"}
    assert "Türkiye" in body["preview"]["en"]


def test_readback_relabels_landed_value_grounding_the_observation() -> None:
    """Fault B: after commit, the SSOT read-back diff re-resolves the landed id 224 to its label,
    leaving nothing for the agent to fabricate."""
    client = _client()
    pid = _propose(client, "resource.update",
                   {"target": "res.partner", "id": "43", "data": {"country_id": 224}})["id"]
    committed = client.post("/nil/v0.1/commit", json={
        "nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"proposal": pid, "idempotency_key": pid}}).json()["body"]
    assert committed["state"] == "executed"
    row = next(r for r in committed["result"]["ssot"]["fields"] if r["field"] == "country_id")
    assert row["after"] == 224
    assert row["after_label"] == "Türkiye (TR)"


def test_unconstrained_field_is_not_labeled() -> None:
    """Legibility touches only constrained fields — a plain scalar carries no label noise."""
    body = _propose(_client(), "resource.update",
                    {"target": "res.partner", "id": "43", "data": {"name": "AHMED"}})
    assert body["resolved"].get("references", {}) == {}
