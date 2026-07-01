"""Real-instance conformance (Phase 4, live half). The synthetic suite (test_module_coverage.py) proves
the machinery against FakeSystem; THIS proves it against a real Odoo over XML-RPC. It is GATED on
ODOO_* credentials and SKIPS cleanly when they are absent (CI without secrets, local dev), so it never
blocks the suite — but the moment an operator exports creds it validates discovery / projection /
governance end-to-end against their actual instance.

Read-only by DEFAULT (safe to run against production). Governed-write/method checks are opt-in via
NIL_LIVE_WRITE=1 and still go through the full propose→commit gate.

Run:  ODOO_URL=… ODOO_DB=… ODOO_LOGIN=… ODOO_API_KEY=… pytest conformance/test_live_odoo.py -v
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import RealSystemClient

_ENV = ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_API_KEY")
_HAVE_CREDS = all(os.environ.get(k) for k in _ENV)
pytestmark = pytest.mark.skipif(not _HAVE_CREDS, reason=f"live Odoo creds absent ({', '.join(_ENV)})")


@pytest.fixture(scope="module")
def client() -> TestClient:
    sys = RealSystemClient(os.environ["ODOO_URL"], db=os.environ["ODOO_DB"],
                           login=os.environ["ODOO_LOGIN"], api_key=os.environ["ODOO_API_KEY"])
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _q(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "live", "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/query", json=env).json()["data"]


def _propose(client: TestClient, verb: str, args: dict) -> dict:
    env = {"nil": "0.1", "grant": "g", "workspace": "live", "body": {"verb": verb, "args": args}}
    return client.post("/nil/v0.1/propose", json=env).json()["body"]


def test_describe_advertises_governed_surface(client: TestClient) -> None:
    body = client.get("/nil/v0.1/describe").json()
    assert "resource.method" in body["verbs"]
    assert body["targets"].get("res.partner", {}).get("exists") is True  # CRM base model provisioned


def test_curated_crm_read_projects_lean(client: TestClient) -> None:
    out = _q(client, "nil.search", {"target": "res.partner", "fields": ["name", "phone"], "limit": 5})
    assert isinstance(out.get("items"), list)
    if out["items"]:
        assert set(out["items"][0]).issubset({"id", "name", "phone"})  # projected, not whole record


def test_count_is_o1_shaped(client: TestClient) -> None:
    out = _q(client, "nil.count", {"target": "res.partner"})
    assert isinstance(out.get("count"), int) and out["count"] >= 0


def test_dynamic_discovery_reads_a_non_crm_model_if_installed(client: TestClient) -> None:
    # account.move has no curated projection — it must be discovered live from fields_get, or refuse
    # cleanly if the accounting module is not installed. Either is conformant; a 500 is not.
    out = _q(client, "nil.count", {"target": "account.move"})
    assert ("count" in out) or out.get("outcome") == "refused", out


def test_governance_denies_generic_write_to_financial_model(client: TestClient) -> None:
    body = _propose(client, "resource.create", {"target": "account.move", "data": {"amount_total": 1}})
    assert body["outcome"] == "refusal"  # default-deny holds against the real instance too


@pytest.mark.skipif(os.environ.get("NIL_LIVE_WRITE") != "1", reason="opt-in live write check (NIL_LIVE_WRITE=1)")
def test_governed_crm_write_roundtrips(client: TestClient) -> None:
    # A reversible CRM write end-to-end on the real instance: create a lead, then ROLLBACK it.
    body = _propose(client, "resource.create",
                    {"target": "crm.lead", "data": {"name": "NIL live conformance — safe to delete"}})
    assert body["outcome"] == "proposal", body
    env = {"nil": "0.1", "grant": "g", "workspace": "live",
           "body": {"proposal": body["id"], "idempotency_key": body["id"]}}
    status = client.post("/nil/v0.1/commit", json=env).json()["body"]
    assert status["state"] == "executed"
    token = status["compensation"]["token"]  # undo it so the instance is left clean
    rb = client.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "live",
                     "body": {"compensation_token": token}}).json()["body"]
    assert rb["outcome"] == "proposal"
    client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "live",
                "body": {"proposal": rb["id"], "idempotency_key": rb["id"]}})
