"""M8/M9: the Odoo PO leg of the dual PO, its reversal, and the landed cost.

Offline, against `FakeSystem` — there is no live Odoo tenant in this suite (the live gate is
`nilscript conformance-test --url <shim>`; a live run against wosool.odoo.com is a separate,
credentialed step).

What the dual PO needs from THIS adapter is not routing — routing (`via: "odoo"` → ActionStep.adapter
→ client.propose(adapter=…)) belongs to the control plane. What it needs is a leg that can be UNDONE
when the sibling leg refuses. That did not exist: `purchase.create_order` was absent from
COMPENSATIONS, so it was IRREVERSIBLE, so the envelope had nothing to compensate the Odoo leg with,
so "refusing the Daftara leg compensates per envelope" could not have happened however the routing
was wired. These tests pin the inverse down and RUN it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from odoo_nil_adapter.compensation import COMPENSATIONS
from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem, SystemError
from odoo_nil_adapter.translate import WRITE_VERBS

PO_VERB = "purchase.create_order"


def _env(verb: str, args: dict) -> dict:
    return {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}


def _shim(system: FakeSystem) -> TestClient:
    return TestClient(
        create_app(system, CapturingEmitter(), bearer=None), raise_server_exceptions=False
    )


def _propose(client: TestClient, verb: str, args: dict) -> dict:
    return client.post("/nil/v0.1/propose", json=_env(verb, args)).json()["body"]


def _commit(client: TestClient, pid: str) -> dict:
    return client.post(
        "/nil/v0.1/commit",
        json={"nil": "0.1", "grant": "g", "workspace": "w",
              "body": {"proposal": pid, "idempotency_key": pid}},
    ).json()["body"]


def _rollback(client: TestClient, token: str) -> dict:
    return client.post(
        "/nil/v0.1/rollback",
        json={"nil": "0.1", "grant": "g", "workspace": "w",
              "body": {"compensation_token": token, "reason": "sibling_leg_refused"}},
    ).json()["body"]


PO_ARGS = {"partner_id": "1", "origin": "REPL-2026-001"}


def test_odoo_po_leg_commits_and_is_reversible() -> None:
    """propose → commit → THE PURCHASE ORDER EXISTS in the backend's own model, and the effect
    declares itself REVERSIBLE with a usable reversal handle."""
    system = FakeSystem()
    client = _shim(system)

    proposed = _propose(client, PO_VERB, PO_ARGS)
    assert proposed["outcome"] == "proposal", proposed

    committed = _commit(client, proposed["id"])
    assert committed["state"] == "executed", committed
    po_id = committed["result"]["entity"]["id"]
    assert system.get("purchase.order", po_id) is not None, (
        "COMMIT said executed but no purchase order exists"
    )
    assert committed["compensation"]["reversibility"] == "REVERSIBLE"
    assert committed["compensation"].get("token")


def test_odoo_po_reversal_actually_removes_the_order() -> None:
    """The saga's unwind, RUN (not previewed): the Daftara leg refuses, the envelope rolls the Odoo
    leg back, and the purchase order is GONE from Odoo. A compensation that is only ever previewed is
    a promise; the previous suite stopped at the preview."""
    system = FakeSystem()
    client = _shim(system)

    committed = _commit(client, _propose(client, PO_VERB, PO_ARGS)["id"])
    po_id = committed["result"]["entity"]["id"]

    preview = _rollback(client, committed["compensation"]["token"])
    assert preview["outcome"] == "proposal", "ROLLBACK must preview, never silently write"
    assert preview["verb"] == "purchase.delete_order"
    assert system.get("purchase.order", po_id) is not None, "the preview must not write"

    reversed_ = _commit(client, preview["id"])
    assert reversed_["state"] == "executed", reversed_
    assert system.get("purchase.order", po_id) is None, (
        "the compensation reported success but the purchase order is still in Odoo"
    )


class _RefusingDeleteSystem(FakeSystem):
    """Odoo refuses to unlink a PO that has been confirmed/received."""

    def delete(self, model: str, record_id: str) -> None:
        raise SystemError("You cannot delete a purchase order which is not draft")


class _SilentNoopDeleteSystem(FakeSystem):
    """The quieter one: the call returns cleanly and removes nothing."""

    def delete(self, model: str, record_id: str) -> None:
        return None


@pytest.mark.parametrize("system_cls", [_RefusingDeleteSystem, _SilentNoopDeleteSystem])
def test_failed_compensation_is_reported_as_failed(system_cls: type[FakeSystem]) -> None:
    """A reversal that did not happen must NEVER be reported as one — neither the refused delete nor
    the accepted-but-did-nothing delete. `executed` here would tell the envelope the saga had unwound
    cleanly while the purchase order was still sitting with the supplier."""
    system = system_cls()
    client = _shim(system)

    committed = _commit(client, _propose(client, PO_VERB, PO_ARGS)["id"])
    po_id = committed["result"]["entity"]["id"]

    preview = _rollback(client, committed["compensation"]["token"])
    outcome = _commit(client, preview["id"])

    assert outcome["state"] == "failed_terminal", (
        f"a compensation that did not land was reported as {outcome.get('state')!r} — "
        "a refused reversal must never be reported as compensated"
    )
    assert outcome.get("compensation") is None
    assert system.get("purchase.order", po_id) is not None


@pytest.mark.parametrize("bad", [{"qty": "0"}, {"qty": "x"}, {"po_amount": "0"}])
def test_landed_cost_uncomputable_is_refused(bad: dict) -> None:
    """`commerce.set_landed_cost` is arithmetic. With no positive quantity there is no per-unit cost,
    and the verb must say so at PROPOSE. It used to translate to an EMPTY doc, write it, skip the
    read-back (no fields written) and report `claim: success` — announcing a cost it had never set."""
    system = FakeSystem()
    client = _shim(system)
    args = {"product_id": "1", "po_amount": "1000", "qty": "10", **bad}

    refused = _propose(client, "commerce.set_landed_cost", args)
    assert refused["outcome"] == "refusal", refused
    assert refused["field"] in ("qty", "po_amount")


def test_landed_cost_computes_and_lands_on_the_product() -> None:
    """(1000 + 100 + 50 + 25) / 10 = 117.5 per unit, witnessed on the product in the backend."""
    system = FakeSystem()
    client = _shim(system)
    pid = str(system.create("product.product", {"name": "Widget"})["name"])

    proposed = _propose(client, "commerce.set_landed_cost", {
        "product_id": pid, "po_amount": "1000", "freight": "100",
        "insurance": "50", "duties": "25", "qty": "10",
    })
    assert proposed["outcome"] == "proposal", proposed
    assert _commit(client, proposed["id"])["state"] == "executed"
    assert system.get("product.product", pid)["standard_price"] == 117.5


def test_describe_declares_tier_and_reversibility_for_every_write_verb() -> None:
    """The control plane's describe is the only real authority (the hub's VERB_DETAILS table is
    display-only). `wrap_cycle` fail-closes an undeclared tier to HIGH and an undeclared reversibility
    to IRREVERSIBLE — so an ABSENT reversibility silently registers a reversible purchase order as one
    that can never be undone. It was absent."""
    d = _shim(FakeSystem()).get("/nil/v0.1/describe").json()
    details = {row["verb"]: row for row in d["verb_details"]}

    assert details[PO_VERB]["reversibility"] == "REVERSIBLE"
    assert details["purchase.delete_order"]["tier"] == "HIGH"
    assert details["purchase.confirm_order"]["reversibility"] == "COMPENSABLE"  # button_cancel
    landed = details["commerce.set_landed_cost"]
    assert landed["required"] == ["product_id", "po_amount", "qty"]
    assert landed["reversibility"] == "IRREVERSIBLE"

    for row in d["verb_details"]:
        assert row["reversibility"] in ("REVERSIBLE", "COMPENSABLE", "IRREVERSIBLE")
        assert row["tier"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def test_every_declared_compensation_is_executable() -> None:
    """A compensating verb this adapter cannot RUN is not a compensation — it is a promise that mints
    a token, previews a reversal, and then fails. Every verb named in COMPENSATIONS must be a curated
    write verb or a `resource.*` op on the generic spine."""
    for name, spec in COMPENSATIONS.items():
        comp = spec.get("verb")
        if comp is None:  # before-image strategy: the edge synthesizes the restore inline
            assert spec.get("strategy") == "before_image", f"{name}: no verb and no strategy"
            continue
        assert comp in WRITE_VERBS or comp.startswith("resource."), (
            f"{name} declares {spec['reversibility']} via {comp!r}, which this adapter cannot "
            f"execute — declare it IRREVERSIBLE or implement the inverse"
        )


def test_manifest_declares_every_write_verb_and_agrees_with_the_code() -> None:
    """The anti-drift gate. `requirements-manifest.json` is what `nilscript manifest validate` gates,
    and it had drifted so far from the code that it was both INCOMPLETE (7 of 21 verbs) and
    STRUCTURALLY INVALID (`reversibility: "CONDITIONAL"`, a tier that does not exist; REVERSIBLE
    entries with no compensation block). The gate had never been run — this repo ships no CI workflow.
    A gate nobody runs is a gate that does not exist."""
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "requirements-manifest.json").read_text()
    )
    declared = manifest["verbs"]

    for name, verb in WRITE_VERBS.items():
        assert name in declared, (
            f"{name} is a shipped write verb but is ABSENT from requirements-manifest.json — "
            "absence means IRREVERSIBLE, and an accident must not be able to look like a decision"
        )
        entry = declared[name]
        assert entry["doctype"] == verb.doctype
        assert entry["tier"] == verb.tier
        assert entry["op"] == verb.op
        assert entry["required"] == list(verb.required)

        if verb.reverse_method:
            expected = "COMPENSABLE"
        else:
            expected = COMPENSATIONS.get(name, {}).get("reversibility", "IRREVERSIBLE")
        assert entry["reversibility"] == expected, (
            f"{name}: the manifest says {entry['reversibility']}, the code says {expected}"
        )
        if expected == "IRREVERSIBLE":
            assert "compensation" not in entry
        else:
            assert entry["compensation"]["verb"]
