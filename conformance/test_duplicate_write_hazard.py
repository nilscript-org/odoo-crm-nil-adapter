"""The duplicate-write hazard (RECOVERY-MATRIX M42): one commit call, TWO records in Odoo.

`_kw` retried `execute_kw` up to 3x on 429/5xx **and on a generic `Exception`** — with no key. A
`create` that Odoo COMMITTED but whose answer was lost (a gateway timeout, a dropped socket, a proxy
502) was therefore repeated, and the second attempt minted a second purchase order. Nothing of ours
crashed: the control plane saw one commit, one receipt, one ledger event, and Odoo held two POs.

The rule these tests pin: **a retry is only safe when the operation is idempotent OR can be checked
first.**

  * read-shaped calls (search / read / search_read / search_count / fields_get) retry as before —
    repeating a read cannot change the world;
  * every other method is write-shaped and is NEVER blindly repeated. Its outcome after a transport
    failure is UNOBSERVED, and an unobserved effect must never be reported as either success or
    failure;
  * a create that carries a queryable attempt key ASKS FIRST — it returns the record that already
    exists instead of creating a second one;
  * a create with no such key REFUSES, naming the doubt, so the layer above parks for a human.
"""

from __future__ import annotations

from typing import Any

import pytest
import xmlrpc.client
from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem, OutcomeInDoubt, RealSystemClient, SystemError
from odoo_nil_adapter.translate import WRITE_VERBS


# ── the system layer: which faults are retried, and for which methods ────────────────────────────
class _FlakyProxy:
    """Odoo's XML-RPC object endpoint: fail the first `fail_times` calls with `exc`, then answer."""

    def __init__(self, exc: Exception, fail_times: int, ok: object = 42) -> None:
        self._exc, self._fail_times, self._ok, self.calls = exc, fail_times, ok, 0

    def execute_kw(self, *_args, **_kw) -> object:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return self._ok


def _client(proxy: _FlakyProxy, **kw: Any) -> RealSystemClient:
    c = RealSystemClient("http://x", db="d", login="l", api_key="k", sleep=lambda _s: None, **kw)
    c._uid = 1  # skip auth
    c._models = proxy
    return c


def test_a_create_whose_answer_is_lost_is_not_silently_repeated() -> None:
    """THE defect. A dropped socket on a `create` used to be retried — minting a second record."""
    proxy = _FlakyProxy(OSError("connection reset by peer"), fail_times=1)
    client = _client(proxy, max_retries=3, backoff=0.0)
    with pytest.raises(OutcomeInDoubt):
        client._kw("purchase.order", "create", [{"partner_id": 1}])
    assert proxy.calls == 1, "the create was repeated — that is the duplicate purchase order"


def test_a_gateway_timeout_on_a_write_is_in_doubt_not_retried() -> None:
    """504 is the textbook lost answer: Odoo may have committed and the gateway gave up waiting."""
    proxy = _FlakyProxy(xmlrpc.client.ProtocolError("u", 504, "Gateway Timeout", {}), fail_times=1)
    client = _client(proxy, max_retries=3, backoff=0.0)
    with pytest.raises(OutcomeInDoubt):
        client._kw("account.move", "create", [{}])
    assert proxy.calls == 1


def test_an_unclassified_method_is_treated_as_a_write() -> None:
    """Fail closed: a method this adapter has not classified as a read is a write. `action_post`
    posts a journal entry — repeating it must never be this layer's decision."""
    proxy = _FlakyProxy(OSError("broken pipe"), fail_times=1)
    client = _client(proxy, max_retries=3, backoff=0.0)
    with pytest.raises(OutcomeInDoubt):
        client._kw("account.move", "action_post", [[1]])
    assert proxy.calls == 1


def test_a_rate_limited_write_is_provably_unapplied_and_still_retried() -> None:
    """429 is the ONE transport answer that proves the call never reached the ORM — the limiter
    rejected it before dispatch. Retrying it is not blind, and the "429 flood" governor keeps working."""
    proxy = _FlakyProxy(xmlrpc.client.ProtocolError("u", 429, "Too Many Requests", {}), fail_times=2)
    client = _client(proxy, max_retries=3, backoff=0.0)
    assert client._kw("purchase.order", "create", [{}]) == 42
    assert proxy.calls == 3


def test_a_persistent_rate_limit_on_a_write_is_terminal_not_in_doubt() -> None:
    """Exhausted 429s mean the write NEVER happened — that is a determinate failure, not a doubt."""
    proxy = _FlakyProxy(xmlrpc.client.ProtocolError("u", 429, "Too Many Requests", {}), fail_times=99)
    client = _client(proxy, max_retries=2, backoff=0.0)
    with pytest.raises(SystemError) as caught:
        client._kw("purchase.order", "create", [{}])
    assert not isinstance(caught.value, OutcomeInDoubt)


@pytest.mark.parametrize("method", ["search", "read", "search_read", "search_count", "fields_get"])
def test_reads_still_retry_as_before(method: str) -> None:
    proxy = _FlakyProxy(xmlrpc.client.ProtocolError("u", 503, "Service Unavailable", {}), fail_times=2)
    client = _client(proxy, max_retries=3, backoff=0.0)
    assert client._kw("crm.lead", method, [[]]) == 42
    assert proxy.calls == 3, f"{method} is a read — repeating it cannot change the world"


def test_a_read_that_never_recovers_is_a_plain_system_error() -> None:
    proxy = _FlakyProxy(OSError("connection reset"), fail_times=99)
    client = _client(proxy, max_retries=2, backoff=0.0)
    with pytest.raises(SystemError) as caught:
        client._kw("crm.lead", "search_read", [[]])
    assert not isinstance(caught.value, OutcomeInDoubt), "a read has no outcome to be in doubt about"


def test_an_application_fault_on_a_write_stays_terminal() -> None:
    """Odoo ANSWERED — the ORM rolled back. That is an observed outcome, not a doubt."""
    proxy = _FlakyProxy(xmlrpc.client.Fault(2, "AccessError: not allowed"), fail_times=99)
    client = _client(proxy, max_retries=3, backoff=0.0)
    with pytest.raises(SystemError) as caught:
        client._kw("purchase.order", "create", [{}])
    assert not isinstance(caught.value, OutcomeInDoubt)
    assert proxy.calls == 1


def test_outcome_in_doubt_is_a_system_error_so_no_layer_ever_500s() -> None:
    assert issubclass(OutcomeInDoubt, SystemError)


# ── the edge: ask first when a key exists, refuse honestly when one does not ──────────────────────
class _LosesTheAnswer(FakeSystem):
    """The backing system COMMITS the record and then the answer is lost on the wire."""

    def __init__(self, *, land: bool = True) -> None:
        super().__init__()
        self._land = land
        self.create_calls = 0

    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        self.create_calls += 1
        if self.create_calls == 1:
            if self._land:
                super().create(target, doc)  # Odoo committed it...
            raise OutcomeInDoubt(  # ...and the answer never came back
                "simulated lost answer", model=target, method="create"
            )
        return super().create(target, doc)


def _app(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _po_system(cls: type[FakeSystem] = FakeSystem, **kw: Any) -> FakeSystem:
    sys = cls(**kw)
    # `origin` is a real, writable Char on purchase.order — the stamp is only applied when the live
    # schema says the field exists, so the fake must declare it exactly as Odoo's fields_get would.
    sys.schemas["purchase.order"] = [{"name": "origin", "type": "char", "required": False,
                                      "readonly": False}]
    return sys


def _commit(client: TestClient, verb: str, args: dict[str, Any], key: str) -> dict[str, Any]:
    env = {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}
    pid = client.post("/nil/v0.1/propose", json=env).json()["body"]["id"]
    return client.post(
        "/nil/v0.1/commit",
        json={"nil": "0.1", "grant": "g", "workspace": "w",
              "body": {"proposal": pid, "idempotency_key": key}},
    ).json()["body"]


def test_the_attempt_key_is_stamped_where_odoo_persists_it() -> None:
    """The NIL attempt key used to die in the adapter's in-process dict. It now reaches Odoo, in a
    field the adapter can query — which is what turns "cannot tell" into "ask first"."""
    sys = _po_system()
    body = _commit(_app(sys), "purchase.create_order", {"partner_id": "7"}, "prep:abc123:r1")
    assert body["state"] == "executed"
    order = sys.docs["purchase.order"][0]
    assert "prep:abc123:r1" in order["origin"], order


def test_a_caller_supplied_origin_is_kept_alongside_the_key() -> None:
    """`origin` is a human-visible Source Document. The key rides with it, never over it."""
    sys = _po_system()
    _commit(_app(sys), "purchase.create_order",
            {"partner_id": "7", "origin": "SEWAR-REPLEN-0001"}, "prep:abc123:r1")
    origin = sys.docs["purchase.order"][0]["origin"]
    assert "SEWAR-REPLEN-0001" in origin and "prep:abc123:r1" in origin, origin


def test_a_second_commit_of_the_same_attempt_returns_the_existing_order() -> None:
    """M41: the adapter's idempotency ledger is a plain dict and dies with the process. The key in
    Odoo outlives it — a re-commit of the SAME attempt must find the order, not mint a second."""
    sys = _po_system()
    first = _commit(_app(sys), "purchase.create_order", {"partner_id": "7"}, "prep:abc123:r1")
    assert first["state"] == "executed"
    # a FRESH app = the adapter restarted; its in-memory ledger remembers nothing
    second = _commit(_app(sys), "purchase.create_order", {"partner_id": "7"}, "prep:abc123:r1")
    assert second["state"] == "executed"
    assert second.get("replayed") is True, "the second commit must announce itself as a replay"
    assert len(sys.docs["purchase.order"]) == 1, "a second purchase order was created"


def test_a_lost_answer_returns_the_order_that_did_land() -> None:
    """The M42 scenario itself: Odoo committed, the answer was lost. Ask first, answer with the
    record that exists — one order, one truth, and no second irreversible act."""
    sys = _po_system(_LosesTheAnswer, land=True)
    body = _commit(_app(sys), "purchase.create_order", {"partner_id": "7"}, "prep:abc123:r1")
    assert body["state"] == "executed"
    assert len(sys.docs["purchase.order"]) == 1
    assert sys.create_calls == 1, "the create was re-issued instead of asked about"


def test_a_lost_answer_with_nothing_written_is_still_in_doubt() -> None:
    """A probe that finds nothing cannot tell "never happened" from "committing right now". The
    honest answer is still doubt — and a re-commit under the same key is now safe."""
    sys = _po_system(_LosesTheAnswer, land=False)
    body = _commit(_app(sys), "purchase.create_order", {"partner_id": "7"}, "prep:abc123:r1")
    assert body["outcome"] == "refusal"
    assert "OUTCOME_IN_DOUBT" in body["message"]


def test_a_probe_blinded_after_the_write_leaves_the_doubt_standing() -> None:
    """The probe reads over the SAME broken transport. If it cannot answer AFTER the write went out,
    reporting `failed_terminal` would assert "it did not happen" on the strength of a read that never
    ran — "nothing here" and "I could not look" must never be the same answer."""

    class _BlindAfterTheWrite(_LosesTheAnswer):
        def search(self, target: str, domain: Any, **kw: Any) -> list[dict[str, Any]]:
            if self.create_calls:  # the ask-first probe still works; the recovery probe does not
                raise SystemError("odoo purchase.order.search_read transport error: reset")
            return super().search(target, domain, **kw)

    sys = _po_system(_BlindAfterTheWrite, land=True)
    body = _commit(_app(sys), "purchase.create_order", {"partner_id": "7"}, "prep:abc123:r1")
    assert body["outcome"] == "refusal", body
    assert "OUTCOME_IN_DOUBT" in body["message"]


def test_a_guard_that_cannot_run_refuses_before_writing_anything() -> None:
    """If the ask-first probe itself cannot run, writing anyway is how the duplicate gets made. So
    nothing is written — and that is a DETERMINATE answer (safe to retry), not a doubt."""

    class _BlindProbe(FakeSystem):
        def search(self, target: str, domain: Any, **kw: Any) -> list[dict[str, Any]]:
            raise SystemError("odoo purchase.order.search_read transport error: reset")

    sys = _po_system(_BlindProbe)
    body = _commit(_app(sys), "purchase.create_order", {"partner_id": "7"}, "prep:abc123:r1")
    assert body["outcome"] == "refusal", body
    assert body["code"] == "UPSTREAM_UNAVAILABLE"
    assert "NOTHING WAS WRITTEN" in body["message"]
    assert not sys.docs.get("purchase.order"), "it wrote without being able to check first"


def test_a_key_that_already_matches_two_records_refuses_instead_of_adding_a_third() -> None:
    """The state M42 leaves behind. Nothing here can tell which of the two this commit owns — so it
    refuses and hands it to a human, rather than making the pile deeper."""
    sys = _po_system()
    marker = "[WSL-prep:abc123:r1]"
    sys.docs["purchase.order"] = [
        {"id": 1, "name": "P00001", "origin": marker, "target": "purchase.order"},
        {"id": 2, "name": "P00002", "origin": marker, "target": "purchase.order"},
    ]
    body = _commit(_app(sys), "purchase.create_order", {"partner_id": "7"}, "prep:abc123:r1")
    assert body["outcome"] == "refusal", body
    assert "OUTCOME_IN_DOUBT" in body["message"]
    assert len(sys.docs["purchase.order"]) == 2, "a third purchase order was created"


def test_a_create_with_no_queryable_key_refuses_naming_the_doubt() -> None:
    """crm.lead carries no field this adapter can stamp an attempt key into. A transport failure
    there is unrecoverable from inside the adapter — so it says so, per verb, rather than guessing."""
    sys = _LosesTheAnswer(land=True)
    body = _commit(_app(sys), "crm.create_lead", {"name": "Acme"}, "prep:abc123:r1")
    assert body["outcome"] == "refusal", body
    assert "OUTCOME_IN_DOUBT" in body["message"]
    assert "crm.lead" in body["message"] and "create" in body["message"]
    assert "no queryable" in body["message"].lower(), body["message"]


def test_an_in_doubt_write_is_never_reported_as_failed_terminal() -> None:
    """`failed_terminal` asserts "it did not happen". After a lost answer nobody knows that."""
    sys = _LosesTheAnswer(land=True)
    body = _commit(_app(sys), "crm.create_lead", {"name": "Acme"}, "prep:abc123:r1")
    assert body.get("state") != "failed_terminal"


def test_the_generic_resource_create_path_also_refuses_in_doubt() -> None:
    sys = _LosesTheAnswer(land=True)
    body = _commit(_app(sys), "resource.create",
                   {"target": "res.partner", "data": {"name": "Acme"}}, "prep:abc:r1")
    assert body["outcome"] == "refusal", body
    assert "OUTCOME_IN_DOUBT" in body["message"]


# ── every create verb, and the honest answer for the ones with no home for a key ─────────────────
_KEYED = sorted(n for n, v in WRITE_VERBS.items() if v.op == "create" and v.idempotency_field)
_UNKEYED = sorted(n for n, v in WRITE_VERBS.items() if v.op == "create" and not v.idempotency_field)


def _placeholder_args(verb_name: str) -> dict[str, Any]:
    verb = WRITE_VERBS[verb_name]
    args: dict[str, Any] = {f: "x" for f in verb.required}
    args.update({f: "10" for f in verb.positive})
    return args


@pytest.mark.parametrize("verb_name", _KEYED)
def test_every_keyed_create_stamps_its_attempt_key(verb_name: str) -> None:
    """Not just the purchase order: whatever a verb declares, the key must actually reach the field."""
    verb = WRITE_VERBS[verb_name]
    sys = FakeSystem()
    sys.schemas[verb.doctype] = [{"name": verb.idempotency_field, "type": "char",
                                  "required": False, "readonly": False}]
    body = _commit(_app(sys), verb_name, _placeholder_args(verb_name), "prep:abc123:r1")
    assert body["state"] == "executed", body
    record = sys.docs[verb.doctype][0]
    assert "prep:abc123:r1" in str(record.get(verb.idempotency_field)), record


@pytest.mark.parametrize("verb_name", _KEYED)
def test_a_model_without_the_declared_field_degrades_to_the_refusal_not_a_bad_write(
    verb_name: str,
) -> None:
    """A declaration is our belief; the live `fields_get` is the instance's truth. An Odoo whose
    model lacks the field must fall back to the honest doubt — never to a write it would reject."""
    verb = WRITE_VERBS[verb_name]
    sys = _LosesTheAnswer(land=True)
    sys.schemas[verb.doctype] = [{"name": "name", "type": "char"}]  # no idempotency field here
    body = _commit(_app(sys), verb_name, _placeholder_args(verb_name), "prep:abc123:r1")
    assert body["outcome"] == "refusal", body
    assert "no queryable" in body["message"].lower()


@pytest.mark.parametrize("verb_name", _UNKEYED)
def test_every_unkeyed_create_says_so_rather_than_guessing(verb_name: str) -> None:
    """crm.lead and product.product have no honest home for an attempt key. Rather than inventing
    one, they say what they cannot do — per verb."""
    sys = _LosesTheAnswer(land=True)
    body = _commit(_app(sys), verb_name, _placeholder_args(verb_name), "prep:abc123:r1")
    assert body["outcome"] == "refusal", body
    assert "OUTCOME_IN_DOUBT" in body["message"]
    assert "no queryable" in body["message"].lower()


def test_the_in_doubt_refusal_is_one_the_KERNEL_CAN_PARSE() -> None:
    """The canary lesson (2026-07-21): a refusal code outside `RefusalCode` makes pydantic raise in
    the SDK and the run dies at the first step — a refusal nobody can read is worse than none. This
    is the positive control on every refusal this edge can emit."""
    from nilscript.sdk.sentences import ProposalBody

    sys = _LosesTheAnswer(land=True)
    body = _commit(_app(sys), "crm.create_lead", {"name": "Acme"}, "prep:abc123:r1")
    parsed = ProposalBody.model_validate(body)  # raises if the code is not a kernel RefusalCode
    assert parsed.is_refusal


def test_every_refusal_code_this_edge_emits_is_a_kernel_refusal_code() -> None:
    """Static guardrail over the whole edge: no future refusal may invent a code the kernel's closed
    enum does not carry."""
    import re
    from pathlib import Path

    from nilscript.sdk.refusals import RefusalCode

    source = Path(__file__).resolve().parents[1] / "src" / "odoo_nil_adapter" / "edge.py"
    codes = set(re.findall(r"_refusal\(\s*env,\s*\"([A-Z_]+)\"", source.read_text()))
    assert codes, "the scanner found no refusals — it would pass even if every code were wrong"
    unknown = sorted(codes - {c.value for c in RefusalCode})
    assert not unknown, f"refusal codes the kernel cannot parse: {unknown}"
