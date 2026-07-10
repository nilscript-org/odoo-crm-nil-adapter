"""Focused proof for the generic entity-noun target resolver added to the Odoo adapter's resource.*
spine: a generic noun (client/customer) resolves to the declared Odoo model (res.partner), and an
exact model name passes straight through. (Odoo's nil.* reads resolve `about` via the IntentResolver
ontology, so this resolver is wired ONLY into the generic resource.* propose/read/method paths.)"""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_nil_adapter import governance
from odoo_nil_adapter.edge import CapturingEmitter, _resolve_target, create_app
from odoo_nil_adapter.system import FakeSystem


def _env(verb: str, args: dict) -> dict:
    return {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}


def test_generic_noun_resolves_to_res_partner() -> None:
    assert _resolve_target("customer") == "res.partner"
    assert _resolve_target("client") == "res.partner"


def test_exact_model_passes_through() -> None:
    for model in governance.writable_targets():
        assert _resolve_target(model) == model


def test_unknown_noun_returned_unchanged() -> None:
    # governance still refuses it downstream; the resolver never invents a target.
    assert _resolve_target("definitely.not.a.model") == "definitely.not.a.model"


def test_aliased_noun_create_commits_into_real_model_not_raw_noun() -> None:
    """The bug this guards: resolution happened at PROPOSE but the COMMIT path reads the STORED
    target. If the stored target were the raw noun ('client'), the CRUD commit would write to a
    phantom model 'client' instead of the resolved 'res.partner'. Assert the record lands in the
    real declared model and NOT under the raw noun. (Odoo's PROPOSE preview echoes the request args,
    so the definitive witness is where the COMMIT actually writes.)"""
    real = _resolve_target("client")
    assert real == "res.partner", "precondition: 'client' resolves to the res.partner model"

    system = FakeSystem()
    client = TestClient(create_app(system, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    proposed = client.post("/nil/v0.1/propose", json=_env(
        "resource.create", {"target": "client", "data": {"name": "Acme"}})).json()["body"]
    assert proposed["outcome"] == "proposal", f"aliased-noun create must propose: {proposed}"

    pid = proposed["id"]
    committed = client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"proposal": pid, "idempotency_key": pid}}).json()["body"]
    assert committed["state"] == "executed", f"aliased-noun create must commit: {committed}"

    assert system.docs.get(real), f"the record must land in the real model '{real}'"
    assert "client" not in system.docs, "commit must NEVER write to the raw noun 'client' (phantom model)"


def test_aliased_noun_method_dispatches_to_real_model_not_raw_noun() -> None:
    """The resource.method commit path also reads the STORED target. Persist the resolved one so a
    governed method proposed against an aliased noun ('client') dispatches to the real model
    ('res.partner'), never the raw noun. `message_post` is granted on res.partner by the default packs."""
    governance.reset_policy()
    real = _resolve_target("client")
    assert real == "res.partner"
    assert governance.method_tier(real, "message_post") is not None, "precondition: message_post granted on res.partner"

    system = FakeSystem()
    system.docs.setdefault(real, []).append({"id": "1", "name": "Acme", "target": real})
    client = TestClient(create_app(system, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    proposed = client.post("/nil/v0.1/propose", json=_env(
        "resource.method", {"target": "client", "id": "1", "method": "message_post",
                            "params": {"body": "hi"}})).json()["body"]
    assert proposed["outcome"] == "proposal", f"aliased-noun method must propose: {proposed}"

    pid = proposed["id"]
    client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"proposal": pid, "idempotency_key": pid}})

    called_targets = {c[0] for c in system.method_calls}
    assert called_targets == {real}, f"the governed method must dispatch to the real model '{real}', got {called_targets}"
    assert "client" not in called_targets, "resource.method must NEVER dispatch to the raw noun 'client'"
