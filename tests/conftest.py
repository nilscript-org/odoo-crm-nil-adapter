"""Shared fixture for the D37 read-plane unit tests: a `fake_client` whose `_plane()` is pre-seeded
with a minimal `ReadBackend` double that records the NATIVE target/domain it last received — the exact
boundary a real Odoo `search_read` call would cross. This lets `test_read_targets.py` and
`test_supplier_resource.py` call `translate._run_nil_*` directly (no FastAPI, no live/fake Odoo) and
still observe that a business name resolved and a base domain was prepended BEFORE anything native-side
ever saw the call — proving the fix at the unit the two tasks actually changed.
"""

from __future__ import annotations

from typing import Any

import pytest

from nilscript.dataplane import Capabilities, FieldSpec, ReadPlane, TargetSchema

from odoo_nil_adapter import translate

# The same triple-op spelling `read_plane._to_domain` produces from a resolved Predicate — kept here,
# independently, so this fixture proves the real op mapping rather than assuming it.
_OP_TO_ODOO: dict[str, str] = {
    "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "in": "in", "contains": "ilike", "ilike": "ilike",
}

_CAPS = Capabilities(server_filter=True, server_sort=True, server_paginate=True, server_aggregate=True)


class _RecordingBackend:
    """A minimal `nilscript.dataplane.ReadBackend` double. Only `res.partner` is a known target — an
    unresolved/unknown target (e.g. a typo'd business name) must still refuse upstream in the ReadPlane
    exactly like a real adapter's `describe_target` returning None, never reach this double at all."""

    def __init__(self) -> None:
        self.last_target: str | None = None
        self.last_domain: list[tuple[str, str, Any]] = []
        self._schemas: dict[str, TargetSchema] = {
            "res.partner": TargetSchema(
                target="res.partner",
                fields=(
                    FieldSpec(name="id", type="str", is_key=True),
                    FieldSpec(name="name", type="str"),
                    FieldSpec(name="email", type="str"),
                    FieldSpec(name="phone", type="str"),
                ),
                cardinality="large",
                default_projection=("id", "name", "email", "phone"),
                capabilities=_CAPS,
            ),
        }

    def describe_target(self, target: str) -> TargetSchema | None:
        return self._schemas.get(target)

    def _domain(self, predicates: Any) -> list[tuple[str, str, Any]]:
        return [(p.field, _OP_TO_ODOO[p.op], p.value) for p in predicates]

    def fetch(self, target, *, predicates, fields, sort, limit, after_id):
        self.last_target = target
        self.last_domain = self._domain(predicates)
        return []

    def count(self, target, *, predicates):
        self.last_target = target
        self.last_domain = self._domain(predicates)
        return 0

    def get_one(self, target, record_id, fields):
        self.last_target = target
        return None

    def aggregate(self, target, *, predicates, group_by, metrics):
        self.last_target = target
        self.last_domain = self._domain(predicates)
        return []


class _FakeClient:
    """Stands in for a `SystemClient`. Never touched by production code directly here — `_plane()`'s
    cache is pre-seeded below, so this only needs to be a distinct, weak-referenceable object."""


@pytest.fixture
def fake_client() -> _FakeClient:
    client = _FakeClient()
    backend = _RecordingBackend()
    client.plane = backend  # the object under test observes calls through here
    # Pre-seed translate's per-client ReadPlane cache so `_plane(client)` returns a plane wrapping OUR
    # recording backend, instead of building the real Odoo-backed one via `build_read_plane`.
    translate._PLANES[client] = ReadPlane(backend, export_store=None)
    return client
