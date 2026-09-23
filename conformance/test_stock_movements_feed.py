"""`history.movements_feed`: Odoo's completed stock moves across ALL products, oldest first, from a
resumable (write_date, id) cursor.

Why a second read beside `history.movements`: that one answers "what happened to THIS product"
(newest first, one product). The engine's observed-external-fact tick needs the other question,
"what has Odoo moved since I last looked", without missing or repeating a move. So:
- the cursor is (write_date, id), not id alone: a move is created when a transfer is confirmed and
  only becomes `done` later, so a low id can complete after a higher one; an id cursor would skip it;
- the cursor is strict on (write_date, id), because one validated transfer writes all its moves with
  the SAME write_date — a write_date-only cursor with a page limit would stall or skip inside it;
- a move written again after `done` comes back again; the consumer dedupes on the move id.

This read only EXPOSES Odoo's data. Deciding what a move means (issue, receipt, adjustment, transfer)
and recording it as a fact is the engine's job, not this adapter's.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from odoo_nil_adapter.edge import CapturingEmitter, create_app
from odoo_nil_adapter.system import FakeSystem, SystemError, domain_matches
from odoo_nil_adapter.translate import QUERY_VERBS

FEED = "history.movements_feed"


def _move(mid: int, product: int, write_date: str, *, state: str = "done", **extra: object) -> dict:
    row: dict = {
        "id": mid, "product_id": [product, f"Product {product}"], "state": state,
        "date": write_date, "write_date": write_date,
        "reference": f"WH/MOVE/{mid:04d}", "quantity": mid, "uom_id": [1, "Units"],
        "location_id": [8, "WH/Stock"], "location_dest_id": [9, "Customers"],
        "location_usage": "internal", "location_dest_usage": "customer",
        "picking_id": [100 + mid, f"WH/OUT/{mid:05d}"], "picking_code": "outgoing",
        "origin": f"SO{mid:03d}", "is_inventory": False, "write_uid": [2, "Mitchell Admin"],
    }
    row.update(extra)
    return row


def _seeded() -> FakeSystem:
    sys = FakeSystem()
    sys.docs["stock.move"] = [
        _move(7, 42, "2026-01-01 10:00:00"),
        # a LOW id that completed LATER than id 7 — an id-only cursor would skip it
        _move(2, 99, "2026-01-02 09:00:00"),
        # three moves of ONE validated transfer share a write_date
        _move(10, 42, "2026-01-03 12:00:00"),
        _move(11, 43, "2026-01-03 12:00:00"),
        _move(12, 44, "2026-01-03 12:00:00"),
        _move(13, 42, "2026-01-04 08:00:00", state="assigned"),  # not done: never a movement
        _move(14, 42, "2026-01-05 08:00:00",
              location_id=[20, "Virtual Locations/Inventory adjustment"],
              location_usage="inventory", location_dest_id=[8, "WH/Stock"],
              location_dest_usage="internal", picking_id=False, picking_code=False,
              is_inventory=True, origin=False),
    ]
    return sys


def _client(sys: FakeSystem) -> TestClient:
    return TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _query(client: TestClient, args: dict) -> dict:
    res = client.post(
        "/nil/v0.1/query",
        json={"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": FEED, "args": args}},
    )
    assert res.status_code == 200, res.text
    return res.json()["data"]


def _ids(data: dict) -> list[int]:
    return [item["id"] for item in data["items"]]


class TestOrderAndCursor:
    def test_all_products_oldest_first_done_only(self) -> None:
        data = _query(_client(_seeded()), {"after_write_date": "2025-12-31T00:00:00Z"})
        assert data["model"] == "stock.move"
        assert _ids(data) == [7, 2, 10, 11, 12, 14], "(write_date, id) ascending; the non-done 13 excluded"
        assert data["count"] == 6
        assert data["complete"] is True

    def test_a_low_id_completed_later_is_not_skipped(self) -> None:
        data = _query(_client(_seeded()),
                      {"after_write_date": "2026-01-01T10:00:00Z", "after_id": "7"})
        assert _ids(data)[0] == 2, "id 2 was written AFTER id 7; the cursor is time first, not id"

    def test_paging_inside_one_write_date_neither_skips_nor_repeats(self) -> None:
        client = _client(_seeded())
        cursor = {"after_write_date": "2026-01-02T09:00:00Z", "after_id": "2"}
        seen: list[int] = []
        for _ in range(5):
            page = _query(client, {**cursor, "limit": "2"})
            seen.extend(_ids(page))
            cursor = {"after_write_date": page["next"]["after_write_date"],
                      "after_id": str(page["next"]["after_id"])}
            if page["complete"]:
                break
        assert seen == [10, 11, 12, 14], "three moves share one write_date; pages of 2 must cover them once"

    def test_next_is_the_last_item_and_an_empty_page_echoes_the_cursor(self) -> None:
        client = _client(_seeded())
        page = _query(client, {"after_write_date": "2026-01-03T12:00:00Z", "after_id": "11", "limit": "1"})
        assert _ids(page) == [12]
        assert page["next"] == {"after_write_date": "2026-01-03T12:00:00Z", "after_id": 12}
        assert page["complete"] is False
        empty = _query(client, {"after_write_date": "2026-02-01T00:00:00Z", "after_id": "0"})
        assert empty["items"] == [] and empty["complete"] is True
        assert empty["next"] == {"after_write_date": "2026-02-01T00:00:00Z", "after_id": 0}


class TestItemsCarryWhatTheConsumerNeeds:
    def test_ids_usages_picking_type_and_writer(self) -> None:
        data = _query(_client(_seeded()), {"after_write_date": "2025-12-31T00:00:00Z"})
        out = next(i for i in data["items"] if i["id"] == 7)
        assert out["product_id"] == 42 and out["product"] == "Product 42"
        assert out["from_id"] == 8 and out["to_id"] == 9
        assert out["from"] == "WH/Stock" and out["to"] == "Customers"
        assert out["from_usage"] == "internal" and out["to_usage"] == "customer"
        assert out["picking_type"] == "outgoing"
        assert out["is_inventory"] is False
        assert out["last_written_by"] == {"id": 2, "name": "Mitchell Admin"}
        assert out["quantity"] == 7 and out["uom"] == "Units"
        assert out["write_date"] == "2026-01-01T10:00:00Z"

    def test_an_inventory_adjustment_move_says_so_and_has_no_picking(self) -> None:
        data = _query(_client(_seeded()), {"after_write_date": "2025-12-31T00:00:00Z"})
        adj = next(i for i in data["items"] if i["id"] == 14)
        assert adj["is_inventory"] is True
        assert adj["from_usage"] == "inventory" and adj["to_usage"] == "internal"
        assert adj["picking"] is None and adj["picking_type"] is None

    def test_a_field_this_instance_lacks_is_reported_missing_never_invented(self) -> None:
        sys = _seeded()
        sys.schemas["stock.move"] = [{"name": n} for n in (
            "id", "product_id", "reference", "quantity", "uom_id", "location_id", "location_dest_id",
            "picking_id", "origin", "date", "write_date", "write_uid",
        )]
        for row in sys.docs["stock.move"]:  # the instance really has none of these
            for gone in ("location_usage", "location_dest_usage", "picking_code", "is_inventory"):
                row.pop(gone, None)
        data = _query(_client(sys), {"after_write_date": "2025-12-31T00:00:00Z"})
        assert set(data["missing_fields"]) == {
            "location_usage", "location_dest_usage", "picking_code", "is_inventory",
        }
        item = data["items"][0]
        assert item["from_usage"] is None and item["is_inventory"] is None, "absent is None, not False"


class TestRefusals:
    def test_missing_cursor_refuses(self) -> None:
        data = _query(_client(_seeded()), {})
        assert data["outcome"] == "refused" and data["code"] == "MISSING_ARGS"

    def test_bad_cursor_date_refuses(self) -> None:
        data = _query(_client(_seeded()), {"after_write_date": "yesterday"})
        assert data["outcome"] == "refused" and data["code"] == "BAD_ARGS"

    def test_bad_cursor_id_refuses(self) -> None:
        data = _query(_client(_seeded()), {"after_write_date": "2026-01-01T00:00:00Z", "after_id": "x"})
        assert data["outcome"] == "refused" and data["code"] == "BAD_ARGS"

    def test_bad_limit_refuses(self) -> None:
        data = _query(_client(_seeded()), {"after_write_date": "2026-01-01T00:00:00Z", "limit": "500"})
        assert data["outcome"] == "refused" and data["code"] == "BAD_ARGS"

    def test_upstream_failure_is_a_refusal_never_an_empty_page(self) -> None:
        sys = _seeded()

        def _boom(*_a: object, **_k: object) -> list:
            raise SystemError("odoo xmlrpc: connection reset")

        sys.search = _boom  # type: ignore[method-assign]
        data = _query(_client(sys), {"after_write_date": "2026-01-01T00:00:00Z"})
        assert data["outcome"] == "refused" and data["code"] == "UPSTREAM_UNAVAILABLE"
        assert "items" not in data and "next" not in data, "a failed look must not advance the cursor"


class TestDeclared:
    def test_advertised_as_a_read_in_describe(self) -> None:
        body = _client(FakeSystem()).get("/nil/v0.1/describe").json()
        details = {d["verb"]: d for d in body["query_verb_details"]}
        assert details[FEED]["effect"] == "read"
        assert details[FEED]["returns"] == "rows"
        assert details[FEED]["required"] == ["after_write_date"]

    def test_in_the_manifest_as_a_read_verb(self) -> None:
        manifest = json.loads(
            (Path(__file__).resolve().parent.parent / "requirements-manifest.json").read_text()
        )
        assert FEED not in manifest["verbs"], "a READ, not a write"
        entry = manifest["read_verbs"][FEED]
        assert entry["effect"] == "read" and entry["returns"] == "rows"
        assert "tier" not in entry and "reversibility" not in entry
        assert entry["required"] == list(QUERY_VERBS[FEED].required)
        assert entry["target"] == QUERY_VERBS[FEED].target


class TestFakeDomainMatchesOdooPrefixNotation:
    """The fake must evaluate the SAME domain Odoo gets, or the cursor tests above prove nothing."""

    ROW = {"write_date": "2026-01-03 12:00:00", "id": 11}

    def test_or_of_and(self) -> None:
        cursor = ["|", ["write_date", ">", "2026-01-03 12:00:00"],
                  "&", ["write_date", "=", "2026-01-03 12:00:00"], ["id", ">", 10]]
        assert domain_matches(self.ROW, cursor)
        assert not domain_matches({**self.ROW, "id": 10}, cursor)
        assert not domain_matches({**self.ROW, "write_date": "2026-01-03 11:59:59"}, cursor)

    def test_implicit_and_and_not(self) -> None:
        assert domain_matches(self.ROW, [["id", "=", 11], ["write_date", ">=", "2026-01-01 00:00:00"]])
        assert not domain_matches(self.ROW, ["!", ["id", "=", 11]])
        assert domain_matches(self.ROW, [])
