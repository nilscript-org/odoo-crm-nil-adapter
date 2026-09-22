"""entity-history E1 follow-up: Odoo answers `history.movements` and `history.changes`.

`stock.move` (the stock ledger) and `mail.message` (chatter) are Odoo's own append-only facts.
Neither was ever read by this adapter — `crm.log_note` posts to `mail.message` but nothing lists
it, and `stock.move`/`stock.move.line` are referenced nowhere in `packs.py`, `translate.py` or
`read_plane.py`. These two READS close that gap, contract frozen in
`docs/superpowers/plans/entity-history-waves/W5-plan.md` ("Contract").

A failed read REFUSES `UPSTREAM_UNAVAILABLE` — it never degrades to `items: []`. "Nothing here"
and "I could not look" must never be the same answer (the bad precedent this deliberately does NOT
copy: `_run_get_contact` swallows `SystemError` into an empty answer).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem, SystemError
from odoo_nil_adapter.translate import QUERY_VERBS

HISTORY_VERBS = ("history.movements", "history.changes")

LONG_NOTE_HTML = "<p>" + ("Called the supplier about the delayed shipment. " * 10) + "</p>"


def _seeded_moves() -> FakeSystem:
    sys = FakeSystem()
    sys.docs["stock.move"] = [
        {
            "id": 1, "product_id": 42, "state": "done",
            "date": "2026-01-01 10:00:00", "write_date": "2026-01-01 10:05:00",
            "reference": "WH/OUT/0001", "quantity": 5, "product_uom": [1, "Units"],
            "location_id": [8, "WH/Stock"], "location_dest_id": [9, "Customers"],
            "picking_id": [3, "WH/OUT/00001"], "origin": "SO001",
        },
        {
            "id": 2, "product_id": 42, "state": "done",
            "date": "2026-01-02 10:00:00", "write_date": "2026-01-02 10:05:00",
            "reference": "WH/OUT/0002", "quantity": 3, "product_uom": [1, "Units"],
            "location_id": [8, "WH/Stock"], "location_dest_id": [9, "Customers"],
            "picking_id": [4, "WH/OUT/00002"], "origin": "SO002",
        },
        {
            "id": 5, "product_id": 42, "state": "done",
            "date": "2026-01-03 10:00:00", "write_date": "2026-01-03 10:05:00",
            "reference": "WH/OUT/0005", "quantity": 7, "product_uom": [1, "Units"],
            "location_id": [8, "WH/Stock"], "location_dest_id": [9, "Customers"],
            "picking_id": [6, "WH/OUT/00005"], "origin": "SO005",
        },
        {  # NOT done — must never be counted as a real movement
            "id": 3, "product_id": 42, "state": "draft",
            "date": "2026-01-04 10:00:00", "write_date": "2026-01-04 10:00:00",
            "reference": "WH/OUT/0003", "quantity": 9, "product_uom": [1, "Units"],
            "location_id": [8, "WH/Stock"], "location_dest_id": [9, "Customers"],
            "picking_id": [7, "WH/OUT/00003"], "origin": "SO003",
        },
        {  # a different product — must never leak into product 42's history
            "id": 4, "product_id": 99, "state": "done",
            "date": "2026-01-05 10:00:00", "write_date": "2026-01-05 10:00:00",
            "reference": "WH/OUT/0004", "quantity": 1, "product_uom": [1, "Units"],
            "location_id": [8, "WH/Stock"], "location_dest_id": [9, "Customers"],
            "picking_id": [8, "WH/OUT/00004"], "origin": "SO004",
        },
    ]
    return sys


def _seeded_moves_odoo19_shape() -> FakeSystem:
    """A stand-in shaped like a REAL Odoo 19 (saas~19.4) `fields_get` (live-gate scouting finding):
    `quantity` carries the moved qty and the unit field is `uom_id` — there is NO `quantity_done`
    and NO `product_uom` on this version at all (an earlier guess that does not exist on 19)."""
    sys = FakeSystem()
    sys.schemas["stock.move"] = [
        {"name": n} for n in (
            "id", "reference", "quantity", "uom_id",
            "location_id", "location_dest_id", "picking_id", "origin", "date", "write_date",
        )
    ]
    sys.docs["stock.move"] = [{
        "id": 1, "product_id": 42, "state": "done",
        "date": "2026-01-01 10:00:00", "write_date": "2026-01-01 10:05:00",
        "reference": "WH/OUT/0001", "quantity": 5, "uom_id": [1, "Units"],
        "location_id": [8, "WH/Stock"], "location_dest_id": [9, "Customers"],
        "picking_id": [3, "WH/OUT/00001"], "origin": "SO001",
    }]
    return sys


def _seeded_moves_odoo16_shape() -> FakeSystem:
    """A stand-in shaped like an OLDER Odoo (16): `quantity_done` carries the moved qty and the
    unit field is `product_uom` — no `quantity`, no `uom_id`."""
    sys = FakeSystem()
    sys.schemas["stock.move"] = [
        {"name": n} for n in (
            "id", "reference", "quantity_done", "product_uom",
            "location_id", "location_dest_id", "picking_id", "origin", "date", "write_date",
        )
    ]
    sys.docs["stock.move"] = [{
        "id": 1, "product_id": 42, "state": "done",
        "date": "2026-01-01 10:00:00", "write_date": "2026-01-01 10:05:00",
        "reference": "WH/OUT/0001", "quantity_done": 5, "product_uom": [1, "Units"],
        "location_id": [8, "WH/Stock"], "location_dest_id": [9, "Customers"],
        "picking_id": [3, "WH/OUT/00001"], "origin": "SO001",
    }]
    return sys


def _seeded_messages() -> FakeSystem:
    sys = FakeSystem()
    sys.docs["mail.message"] = [
        {
            "id": 10, "model": "res.partner", "res_id": 7,
            "date": "2026-02-01 09:00:00", "write_date": "2026-02-01 09:00:00",
            "message_type": "comment", "subtype_id": [2, "Note"],
            "author_id": [5, "Sara"], "subject": "Called supplier", "body": LONG_NOTE_HTML,
        },
        {
            "id": 11, "model": "res.partner", "res_id": 7,
            "date": "2026-02-02 09:00:00", "write_date": "2026-02-02 09:00:00",
            "message_type": "email", "subtype_id": [3, "Discussion"],
            "author_id": False, "subject": "Auto note", "body": "<p>ok</p>",
        },
        {  # a different record on the same model — must never leak in
            "id": 12, "model": "res.partner", "res_id": 999,
            "date": "2026-02-03 09:00:00", "write_date": "2026-02-03 09:00:00",
            "message_type": "comment", "subtype_id": [2, "Note"],
            "author_id": [5, "Sara"], "subject": "Wrong partner", "body": "<p>x</p>",
        },
        {  # the same res_id, wrong model — must never leak in
            "id": 13, "model": "crm.lead", "res_id": 7,
            "date": "2026-02-04 09:00:00", "write_date": "2026-02-04 09:00:00",
            "message_type": "comment", "subtype_id": [2, "Note"],
            "author_id": [5, "Sara"], "subject": "Wrong model", "body": "<p>x</p>",
        },
    ]
    return sys


def _seeded_product_messages() -> FakeSystem:
    """A stand-in that mirrors Odoo's real template/variant split: the product VARIANT
    (`product.product`, id 55) links to its TEMPLATE (`product.tmpl_id` -> 77, the many2one shape
    XML-RPC hands back), and the chatter that matters lives on the TEMPLATE — exactly the live-gate
    scouting finding (`Product` -> `product.product`, but chatter is normally posted on
    `product.template`)."""
    sys = FakeSystem()
    sys.docs["product.product"] = [{"id": 55, "product_tmpl_id": [77, "Widget"]}]
    sys.docs["mail.message"] = [
        {
            "id": 20, "model": "product.product", "res_id": 55,
            "date": "2026-03-01 08:00:00", "write_date": "2026-03-01 08:00:00",
            "message_type": "comment", "subtype_id": [2, "Note"],
            "author_id": [5, "Sara"], "subject": "Variant note", "body": "<p>variant chatter</p>",
        },
        {  # the TEMPLATE's own chatter — where a real Odoo product's messages normally live
            "id": 21, "model": "product.template", "res_id": 77,
            "date": "2026-03-02 08:00:00", "write_date": "2026-03-02 08:00:00",
            "message_type": "comment", "subtype_id": [2, "Note"],
            "author_id": [6, "Nour"], "subject": "Template note", "body": "<p>template chatter</p>",
        },
        {  # a DIFFERENT template — must never leak in
            "id": 22, "model": "product.template", "res_id": 999,
            "date": "2026-03-03 08:00:00", "write_date": "2026-03-03 08:00:00",
            "message_type": "comment", "subtype_id": [2, "Note"],
            "author_id": [6, "Nour"], "subject": "Wrong template", "body": "<p>x</p>",
        },
    ]
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


class TestHistoryMovements:
    def test_newest_first_and_only_done_moves(self) -> None:
        data = _query(_client(_seeded_moves()), "history.movements",
                      {"resource": "Product", "record_id": "42"})
        assert data["resource"] == "Product"
        assert data["record_id"] == "42"
        assert data["model"] == "stock.move"
        ids = [item["id"] for item in data["items"]]
        assert ids == [5, 2, 1], "newest first; draft (3) and another product's move (4) excluded"
        assert data["count"] == 3
        first = data["items"][0]
        assert first["at"] == "2026-01-03T10:00:00Z"
        assert first["reference"] == "WH/OUT/0005"
        assert first["quantity"] == 7
        assert first["uom"] == "Units"
        assert first["from"] == "WH/Stock"
        assert first["to"] == "Customers"
        assert first["picking"] == "WH/OUT/00005"
        assert first["origin"] == "SO005"
        assert first["write_date"] == "2026-01-03T10:05:00Z"

    def test_before_excludes_rows_at_or_after_it(self) -> None:
        data = _query(_client(_seeded_moves()), "history.movements",
                      {"resource": "Product", "record_id": "42", "before": "2026-01-03T10:00:00Z"})
        ids = [item["id"] for item in data["items"]]
        assert ids == [2, 1], "the row AT `before` (id 5) must be excluded, not just rows strictly after"

    def test_limit_caps(self) -> None:
        data = _query(_client(_seeded_moves()), "history.movements",
                      {"resource": "Product", "record_id": "42", "limit": "2"})
        assert [item["id"] for item in data["items"]] == [5, 2]
        assert data["limit"] == 2

    def test_bad_limit_refuses(self) -> None:
        data = _query(_client(_seeded_moves()), "history.movements",
                      {"resource": "Product", "record_id": "42", "limit": "0"})
        assert data["outcome"] == "refused" and data["code"] == "BAD_ARGS"

    def test_bad_before_refuses(self) -> None:
        data = _query(_client(_seeded_moves()), "history.movements",
                      {"resource": "Product", "record_id": "42", "before": "not-a-date"})
        assert data["outcome"] == "refused" and data["code"] == "BAD_ARGS"

    def test_non_product_resource_is_unknown(self) -> None:
        data = _query(_client(_seeded_moves()), "history.movements",
                      {"resource": "Supplier", "record_id": "42"})
        assert data["outcome"] == "refused" and data["code"] == "UNKNOWN_RESOURCE"


class TestHistoryChanges:
    def test_filtered_by_model_and_res_id_for_supplier(self) -> None:
        data = _query(_client(_seeded_messages()), "history.changes",
                      {"resource": "Supplier", "record_id": "7"})
        assert data["resource"] == "Supplier"
        assert data["record_id"] == "7"
        assert data["model"] == "mail.message"
        ids = [item["id"] for item in data["items"]]
        assert ids == [11, 10], "newest first; wrong-record (12) and wrong-model (13) excluded"

    def test_html_body_becomes_plain_text_bounded_to_280(self) -> None:
        data = _query(_client(_seeded_messages()), "history.changes",
                      {"resource": "Supplier", "record_id": "7"})
        note = next(item for item in data["items"] if item["id"] == 10)
        assert "<" not in note["body"] and ">" not in note["body"]
        assert len(note["body"]) <= 280
        assert note["message_type"] == "comment"
        assert note["subtype"] == "Note"
        assert note["author"] == "Sara"
        assert note["subject"] == "Called supplier"

    def test_absent_author_is_null_not_fabricated(self) -> None:
        data = _query(_client(_seeded_messages()), "history.changes",
                      {"resource": "Supplier", "record_id": "7"})
        auto = next(item for item in data["items"] if item["id"] == 11)
        assert auto["author"] is None

    def test_unknown_resource_refuses(self) -> None:
        data = _query(_client(_seeded_messages()), "history.changes",
                      {"resource": "Nope", "record_id": "7"})
        assert data["outcome"] == "refused" and data["code"] == "UNKNOWN_RESOURCE"

    def test_non_numeric_record_id_refuses(self) -> None:
        data = _query(_client(_seeded_messages()), "history.changes",
                      {"resource": "Supplier", "record_id": "abc"})
        assert data["outcome"] == "refused" and data["code"] == "BAD_ARGS"

    def test_missing_record_id_refuses(self) -> None:
        data = _query(_client(_seeded_messages()), "history.changes", {"resource": "Supplier"})
        assert data["outcome"] == "refused" and data["code"] == "MISSING_ARGS"


class TestUpstreamFailureIsARefusalNeverAnEmptyList:
    def test_a_search_that_raises_refuses_UPSTREAM_UNAVAILABLE(self) -> None:
        sys = _seeded_moves()

        def _boom(*_a: object, **_k: object) -> list:
            raise SystemError("odoo xmlrpc: connection reset")

        sys.search = _boom  # type: ignore[method-assign]
        data = _query(_client(sys), "history.movements", {"resource": "Product", "record_id": "42"})
        assert data["outcome"] == "refused"
        assert data["code"] == "UPSTREAM_UNAVAILABLE"
        assert "items" not in data, "'nothing here' and 'I could not look' must never be the same answer"

    def test_changes_upstream_failure_also_refuses(self) -> None:
        sys = _seeded_messages()

        def _boom(*_a: object, **_k: object) -> list:
            raise SystemError("odoo xmlrpc: connection reset")

        sys.search = _boom  # type: ignore[method-assign]
        data = _query(_client(sys), "history.changes", {"resource": "Supplier", "record_id": "7"})
        assert data["outcome"] == "refused" and data["code"] == "UPSTREAM_UNAVAILABLE"


class TestDeclaredEverywhere:
    def test_both_verbs_are_declared_reads_in_describe(self) -> None:
        body = _client(FakeSystem()).get("/nil/v0.1/describe").json()
        details = {d["verb"]: d for d in body["query_verb_details"]}
        for verb in HISTORY_VERBS:
            assert verb in details, f"{verb} must be advertised in describe"
            assert details[verb]["effect"] == "read"
            assert details[verb]["returns"] == "rows"
            assert details[verb]["required"] == ["resource", "record_id"]

    def test_describe_declares_history_movements_and_changes_true(self) -> None:
        body = _client(FakeSystem()).get("/nil/v0.1/describe").json()
        assert body["history"] == {
            "current": True, "changes": True, "movements": True, "messages": False,
        }

    def test_both_verbs_are_in_the_manifest_read_verbs(self) -> None:
        manifest = _manifest()
        for verb in HISTORY_VERBS:
            assert verb not in manifest["verbs"], f"{verb} is a READ, not a write"
            entry = manifest["read_verbs"][verb]
            assert entry["effect"] == "read"
            assert entry["returns"] == "rows"
            assert "tier" not in entry and "reversibility" not in entry
            assert entry["required"] == list(QUERY_VERBS[verb].required)
            assert entry["target"] == QUERY_VERBS[verb].target


class TestProductChangesReadsBothVariantAndTemplate:
    """Live-gate scouting finding: `Product` -> `product.product`, but Odoo posts a product's
    chatter on `product.template`. `history.changes` for a Product must read BOTH and merge —
    otherwise it silently comes back empty on a real instance ("nothing happened" read as "I
    looked in the wrong place"), while `describe()` says `changes: true`."""

    def test_merges_variant_and_template_chatter_newest_first(self) -> None:
        data = _query(_client(_seeded_product_messages()), "history.changes",
                      {"resource": "Product", "record_id": "55"})
        assert data["resource"] == "Product"
        assert data["record_id"] == "55"
        assert data["model"] == "mail.message"
        ids = [item["id"] for item in data["items"]]
        # id 21 lives on the TEMPLATE (product.template/77) — WITHOUT the fix, a domain of
        # model=product.product, res_id=55 alone would never find it, and the answer would be
        # [20] (or even [] if the variant itself carries no chatter): a false "nothing happened".
        assert ids == [21, 20], "the template's own chatter (21) must be found, not just the variant's (20)"
        assert data["count"] == 2

    def test_the_wrong_templates_chatter_never_leaks_in(self) -> None:
        data = _query(_client(_seeded_product_messages()), "history.changes",
                      {"resource": "Product", "record_id": "55"})
        ids = {item["id"] for item in data["items"]}
        assert 22 not in ids

    def test_limit_still_bounds_the_merged_result(self) -> None:
        data = _query(_client(_seeded_product_messages()), "history.changes",
                      {"resource": "Product", "record_id": "55", "limit": "1"})
        assert [item["id"] for item in data["items"]] == [21]
        assert data["limit"] == 1

    def test_dedup_by_message_id_across_variant_and_template(self) -> None:
        sys = _seeded_product_messages()
        original_search = sys.search
        mail_calls: list[tuple[str, list]] = []

        def _search(target: str, domain: list, **kw: object) -> list:
            if target == "mail.message":
                mail_calls.append((target, domain))
                # simulate the SAME message id surfacing from both the variant and template
                # domain calls — dedup must collapse it to one item, never double-count.
                return [{
                    "id": 20, "model": "product.product", "res_id": 55,
                    "date": "2026-03-01 08:00:00", "write_date": "2026-03-01 08:00:00",
                    "message_type": "comment", "subtype_id": [2, "Note"], "author_id": [5, "Sara"],
                    "subject": "Variant note", "body": "<p>variant chatter</p>",
                }]
            return original_search(target, domain, **kw)  # type: ignore[arg-type]

        sys.search = _search  # type: ignore[method-assign]
        data = _query(_client(sys), "history.changes", {"resource": "Product", "record_id": "55"})
        assert [item["id"] for item in data["items"]] == [20]
        assert data["count"] == 1
        assert len(mail_calls) == 2, "both the variant AND the template domain must actually be queried"

    def test_template_resolution_failure_refuses_not_an_empty_list(self) -> None:
        sys = _seeded_product_messages()
        original_search = sys.search

        def _boom(target: str, domain: list, **kw: object) -> list:
            if target == "product.product":
                raise SystemError("odoo xmlrpc: connection reset")
            return original_search(target, domain, **kw)  # type: ignore[arg-type]

        sys.search = _boom  # type: ignore[method-assign]
        data = _query(_client(sys), "history.changes", {"resource": "Product", "record_id": "55"})
        assert data["outcome"] == "refused"
        assert data["code"] == "UPSTREAM_UNAVAILABLE"
        assert "items" not in data, "an unresolved template must never be reported as 'no history'"

    def test_variant_not_found_proceeds_on_variant_domain_alone_no_refusal(self) -> None:
        # the variant genuinely does not exist (no rows, no exception) — a legitimate "nothing to
        # resolve", not a failed read; must not refuse.
        data = _query(_client(_seeded_product_messages()), "history.changes",
                      {"resource": "Product", "record_id": "12345"})
        assert data.get("outcome") != "refused"
        assert data["items"] == []


class TestMissingFieldsAreVisibleNotSilentlyDropped:
    def test_movements_missing_fields_are_reported(self) -> None:
        sys = _seeded_moves()
        # `quantity` alone is enough to satisfy the quantity GROUP (its alternate `quantity_done`
        # need not also exist) — but NEITHER uom spelling is present, so that group IS a real gap,
        # reported once under its primary name. `location_id`/`location_dest_id`/`picking_id`/
        # `origin` have no alternates at all: plain, genuine gaps.
        sys.schemas["stock.move"] = [
            {"name": n} for n in ("id", "date", "write_date", "reference", "quantity")
        ]
        data = _query(_client(sys), "history.movements", {"resource": "Product", "record_id": "42"})
        assert set(data["missing_fields"]) == {
            "uom_id", "location_id", "location_dest_id", "picking_id", "origin",
        }
        assert data["items"], "pruning degrades the answer, it must not empty it"
        assert data["items"][0]["quantity"] == 7, "the group's PRESENT alternate must still be used"

    def test_changes_missing_fields_are_reported(self) -> None:
        sys = _seeded_messages()
        sys.schemas["mail.message"] = [
            {"name": n} for n in ("id", "date", "write_date", "message_type", "model", "res_id")
        ]
        data = _query(_client(sys), "history.changes", {"resource": "Supplier", "record_id": "7"})
        assert set(data["missing_fields"]) == {"subtype_id", "author_id", "subject", "body"}

    def test_no_missing_fields_when_schema_is_unknown(self) -> None:
        data = _query(_client(_seeded_moves()), "history.movements",
                      {"resource": "Product", "record_id": "42"})
        assert data["missing_fields"] == []


class TestMovementsFieldNamesAcrossOdooVersions:
    """Live-gate scouting on a real Odoo 19 (saas~19.4) via `fields_get`: `stock.move` has NO
    `quantity_done` and NO `product_uom` — the unit field is `uom_id` (`quantity` already carries
    the moved qty in 17+). A version-renamed field is not a gap: a healthy instance, whichever
    spelling it uses, must resolve the SAME logical `quantity`/`uom` and report an EMPTY
    `missing_fields` — not the false alarm a flat curated-field list would raise."""

    def test_odoo19_shape_resolves_quantity_and_uom_id_with_no_reported_gap(self) -> None:
        data = _query(_client(_seeded_moves_odoo19_shape()), "history.movements",
                      {"resource": "Product", "record_id": "42"})
        item = data["items"][0]
        assert item["quantity"] == 5
        assert item["uom"] == "Units"
        assert data["missing_fields"] == []

    def test_odoo16_shape_resolves_quantity_done_and_product_uom_with_no_reported_gap(self) -> None:
        data = _query(_client(_seeded_moves_odoo16_shape()), "history.movements",
                      {"resource": "Product", "record_id": "42"})
        item = data["items"][0]
        assert item["quantity"] == 5
        assert item["uom"] == "Units"
        assert data["missing_fields"] == []
