"""THE DOCUMENT IS THE ERP'S, NOT OURS.

Creating a purchase order in Odoo returns the RECORD, never the document: the PDF a vendor is
supposed to receive is rendered SEPARATELY, by Odoo's own QWeb report (`purchase.report_purchaseorder`
/ `account.report_invoice`), with Odoo's numbering, taxes, terms, logo and legal footer. A platform
that re-renders its own PDF from the record sends the vendor a document that is NOT the one in the
system of record — a fabrication, and one that breaks byte-identity between what a human approves
and what the vendor receives.

So the adapter grows a READ verb that returns the ERP's OWN rendered bytes:

    purchase.get_order_document   → purchase.order   via purchase.report_purchaseorder
    account.get_invoice_document  → account.move     via account.report_invoice

It returns the bytes (base64), their content_type, a filename taken from the record's own reference,
and the **sha256 of those exact bytes** — the hash the ledger attests the send with.

These verbs are READS. They must be shaped as reads EVERYWHERE they are declared: absent from
WRITE_VERBS, absent from COMPENSATIONS (where absence means IRREVERSIBLE), absent from the manifest's
write-verb map (same fail-closed default), and present in the read surfaces — with an explicit
`effect: "read"`, never a tier and never a reversibility.

The LIVE render against a real Odoo is UNWITNESSED (no sandbox credentials in this environment):
everything below is witnessed against FakeSystem. See `system.RealSystemClient.render_report`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odoo_nil_adapter.compensation import COMPENSATIONS
from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem, SystemError
from odoo_nil_adapter.translate import QUERY_VERBS, WRITE_VERBS

PDF = b"%PDF-1.4\nthe ERP's own rendered purchase order\n%%EOF\n"
DOCUMENT_VERBS = ("purchase.get_order_document", "account.get_invoice_document")


def _seeded() -> FakeSystem:
    sys = FakeSystem()
    sys.docs["purchase.order"] = [{"id": 42, "name": "P00042", "partner_id": [7, "Sewar"]}]
    sys.docs["account.move"] = [{"id": 9, "name": "BILL/2026/0009", "move_type": "in_invoice"}]
    sys.reports[("purchase.report_purchaseorder", "42")] = PDF
    sys.reports[("account.report_invoice", "9")] = PDF
    return sys


def _client(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _query(client: TestClient, verb: str, args: dict) -> dict:
    res = client.post(
        "/nil/v0.1/query",
        json={"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}},
    )
    assert res.status_code == 200, res.text
    return res.json()["data"]


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parent.parent / "requirements-manifest.json").read_text())


class TestTheDocumentComesFromTheERP:
    def test_the_purchase_order_document_is_the_erps_own_bytes_hashed(self) -> None:
        sys = _seeded()
        doc = _query(_client(sys), "purchase.get_order_document", {"order_id": "42"})["document"]
        assert base64.b64decode(doc["content_base64"]) == PDF  # the ERP's bytes, not ours
        assert doc["sha256"] == hashlib.sha256(PDF).hexdigest()
        assert doc["content_type"] == "application/pdf"
        assert doc["filename"] == "P00042.pdf"  # the record's OWN reference, never minted here
        assert doc["size"] == len(PDF)
        assert doc["source"] == {
            "system": "odoo_crm",
            "report": "purchase.report_purchaseorder",
            "target": "purchase.order",
            "id": "42",
        }
        # the render was asked of Odoo — this adapter composed nothing
        assert sys.report_calls == [("purchase.report_purchaseorder", "purchase.order", "42")]

    def test_the_vendor_bill_document_is_the_erps_own_bytes(self) -> None:
        sys = _seeded()
        doc = _query(_client(sys), "account.get_invoice_document", {"invoice_id": "9"})["document"]
        assert base64.b64decode(doc["content_base64"]) == PDF
        assert doc["sha256"] == hashlib.sha256(PDF).hexdigest()
        assert doc["filename"] == "BILL_2026_0009.pdf"
        assert sys.report_calls == [("account.report_invoice", "account.move", "9")]


class TestFailureIsARefusalNeverAFabrication:
    def test_a_render_that_fails_REFUSES_and_returns_no_document(self) -> None:
        sys = _seeded()

        def _boom(report_ref: str, target: str, record_id: str) -> bytes:
            raise SystemError("odoo ir.actions.report: report not found")

        sys.render_report = _boom  # type: ignore[method-assign]
        data = _query(_client(sys), "purchase.get_order_document", {"order_id": "42"})
        assert data["outcome"] == "refused"
        assert data["code"] == "DOCUMENT_UNAVAILABLE"
        assert "document" not in data  # "I could not fetch it" is never "here it is"

    def test_empty_bytes_are_not_a_document(self) -> None:
        sys = _seeded()
        sys.reports[("purchase.report_purchaseorder", "42")] = b""
        data = _query(_client(sys), "purchase.get_order_document", {"order_id": "42"})
        assert data["outcome"] == "refused" and data["code"] == "DOCUMENT_UNAVAILABLE"

    def test_an_unknown_order_refuses(self) -> None:
        data = _query(_client(_seeded()), "purchase.get_order_document", {"order_id": "999"})
        assert data["outcome"] == "refused" and data["code"] == "DOCUMENT_UNAVAILABLE"

    def test_a_missing_required_arg_refuses_instead_of_crashing(self) -> None:
        data = _query(_client(_seeded()), "purchase.get_order_document", {})
        assert data["outcome"] == "refused" and data["code"] == "MISSING_ARGS"
        assert "order_id" in data["message"]


class TestItIsDeclaredAsAReadEverywhere:
    @pytest.mark.parametrize("verb", DOCUMENT_VERBS)
    def test_it_is_a_query_verb_not_a_write_verb(self, verb: str) -> None:
        assert verb in QUERY_VERBS
        assert verb not in WRITE_VERBS  # a read must never enter the effect plane
        assert verb not in COMPENSATIONS  # absence here means IRREVERSIBLE — for EFFECTS only

    @pytest.mark.parametrize("verb", DOCUMENT_VERBS)
    def test_the_manifest_declares_it_as_a_read_with_no_tier_and_no_reversibility(self, verb: str) -> None:
        manifest = _manifest()
        assert verb not in manifest["verbs"], (
            f"{verb} is a READ: listing it among the write verbs makes 'no reversibility declared' "
            "mean IRREVERSIBLE — a non-effecting call would register as an unreversible effect"
        )
        entry = manifest["read_verbs"][verb]
        assert entry["effect"] == "read"
        assert entry["returns"] == "document"
        assert "tier" not in entry and "reversibility" not in entry
        assert entry["required"] == list(QUERY_VERBS[verb].required)
        assert entry["target"] == QUERY_VERBS[verb].target
        assert entry["report"] == QUERY_VERBS[verb].report

    @pytest.mark.parametrize("verb", DOCUMENT_VERBS)
    def test_describe_advertises_it_as_a_non_effecting_read(self, verb: str) -> None:
        body = _client(_seeded()).get("/nil/v0.1/describe").json()
        assert verb in body["verbs"]
        details = {d["verb"]: d for d in body["query_verb_details"]}
        assert details[verb]["effect"] == "read"
        assert details[verb]["returns"] == "document"
        assert details[verb]["target"] == QUERY_VERBS[verb].target
        assert details[verb]["required"] == list(QUERY_VERBS[verb].required)
        assert verb not in {d["verb"] for d in body["verb_details"]}  # never in the effect contract
