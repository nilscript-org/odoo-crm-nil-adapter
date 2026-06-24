"""State-witness (TOCTOU): a proposal is bound to the SSOT values it previewed. If the world drifts
between PROPOSE and COMMIT (a delayed approval against a changed record), COMMIT fails closed with
PRECONDITION_FAILED and writes nothing — instead of clobbering a concurrent edit with stale intent."""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_crm_nil_adapter.edge import CapturingEmitter, create_app
from odoo_crm_nil_adapter.system import FakeSystem


def _seeded() -> FakeSystem:
    sys = FakeSystem()
    sys.schemas["res.partner"] = [{"name": "phone", "type": "char"}]
    sys.docs["res.partner"] = [{"name": "40", "id": 40, "phone": "111", "target": "res.partner"}]
    return sys


def _client(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _propose(client: TestClient) -> str:
    env = {"nil": "0.1", "grant": "g", "workspace": "w",
           "body": {"verb": "resource.update", "args": {"target": "res.partner", "id": "40", "data": {"phone": "222"}}}}
    return client.post("/nil/v0.1/propose", json=env).json()["body"]["id"]


def _commit(client: TestClient, pid: str) -> dict:
    return client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
                       "body": {"proposal": pid, "idempotency_key": pid}}).json()["body"]


def test_commit_after_state_drift_fails_closed() -> None:
    sys = _seeded()
    client = _client(sys)
    pid = _propose(client)  # binds witness over phone="111"
    sys.docs["res.partner"][0]["phone"] = "999"  # the world changes before commit (concurrent edit)
    committed = _commit(client, pid)
    assert committed["outcome"] == "refusal"
    assert committed["code"] == "PRECONDITION_FAILED"
    # and nothing was written: the drifted value stands, the stale intent did NOT land
    assert sys.docs["res.partner"][0]["phone"] == "999"


def test_commit_without_drift_executes() -> None:
    sys = _seeded()
    client = _client(sys)
    pid = _propose(client)
    committed = _commit(client, pid)  # no drift between propose and commit
    assert committed["state"] == "executed"
    assert sys.docs["res.partner"][0]["phone"] == "222"
