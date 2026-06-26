"""The read data plane for the Odoo shim: maps the universal `nilscript.dataplane.ReadBackend`
protocol onto Odoo's `search_read` (with explicit `fields=` projections), `search_count`, and keyset
paging. The governance — lean projection, byte-cap-refuse, capability fallback, read authz, export
handles, bulk gating — is inherited from the shared `ReadPlane`; this file only does the native mapping.

This is the durable fix for the 590 KB flood: `crm.list_contacts` dumped whole `res.partner` records;
`nil.search` returns `id,name,phone,email` and pages a 1M set the same way it pages 41.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

from nilscript.dataplane import (
    Capabilities,
    ExportStore,
    FieldSpec,
    Predicate,
    ReadPlane,
    TargetSchema,
)

from odoo_crm_nil_adapter.system import SystemClient

# Curated lean projections per CRM target — what an agent actually needs, never the whole record.
# (id is always retained by projection.) Widen deliberately as real flows need more fields.
_TARGET_FIELDS: dict[str, tuple[str, ...]] = {
    # Lean set guaranteed to exist on every res.partner (company_name/country_id are not universally
    # present — a projection must never name a field the model lacks, or search_read 500s).
    "res.partner": ("id", "name", "phone", "email"),
    "crm.lead": ("id", "name", "contact_name", "email_from", "phone", "stage_id", "expected_revenue"),
    "crm.stage": ("id", "name", "sequence"),
    "crm.team": ("id", "name"),
    "res.country": ("id", "name", "code"),
}
_SENSITIVE: dict[str, frozenset[str]] = {
    "res.partner": frozenset({"credit_limit", "vat"}),
}
# Odoo speaks all of these server-side via search_read / search_count / read_group.
_ODOO_CAPS = Capabilities(server_filter=True, server_sort=True, server_paginate=True, server_aggregate=True)

_OP_TO_ODOO = {
    "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "in": "in", "contains": "ilike", "ilike": "ilike",
}


def _to_domain(predicates: Sequence[Predicate]) -> list[list[Any]]:
    """NIL typed predicates → an Odoo AND-of-triples domain. `between` expands to two bounds."""
    domain: list[list[Any]] = []
    for p in predicates:
        if p.op == "between":
            domain.append([p.field, ">=", p.value[0]])
            domain.append([p.field, "<=", p.value[1]])
        else:
            domain.append([p.field, _OP_TO_ODOO[p.op], p.value])
    return domain


class OdooReadBackend:
    """Adapts a `SystemClient` to the universal `ReadBackend` protocol (native I/O only)."""

    def __init__(self, client: SystemClient) -> None:
        self._client = client

    def describe_target(self, target: str) -> TargetSchema | None:
        fields = _TARGET_FIELDS.get(target)
        if fields is None:
            return None
        sensitive = _SENSITIVE.get(target, frozenset())
        specs = tuple(
            FieldSpec(
                name=f, type="str", is_key=(f == "id"),
                sensitivity="sensitive" if f in sensitive else "normal",
            )
            for f in fields
        )
        return TargetSchema(
            target=target, fields=specs, cardinality="large",
            default_projection=fields, capabilities=_ODOO_CAPS,
        )

    def fetch(self, target, *, predicates, fields, sort, limit, after_id):
        domain = _to_domain(predicates)
        if after_id is not None:  # keyset paging: stable for 1M+, no offset drift
            domain = [["id", ">", after_id], *domain]
        cols = tuple(dict.fromkeys(("id", *fields)))
        return self._client.search(target, domain, fields=cols, limit=limit, order="id asc")

    def count(self, target, *, predicates):
        return self._client.count(target, _to_domain(predicates))

    def get_one(self, target, record_id, fields):
        cols = tuple(dict.fromkeys(("id", *fields)))
        rows = self._client.search(target, [["id", "=", record_id]], fields=cols, limit=1)
        return rows[0] if rows else None

    def aggregate(self, target, *, predicates, group_by, metrics):
        # Group via a bounded native pull keyed on `group_by` (the shim's stand-in for read_group);
        # the engine only reaches here when server_aggregate is advertised.
        rows = self._client.search(target, _to_domain(predicates), fields=(group_by, "id"), limit=100_000)
        buckets: dict[Any, int] = {}
        for r in rows:
            key = r.get(group_by)
            if isinstance(key, (list, tuple)):  # Odoo many2one comes back as [id, label] → group by label
                key = key[1] if len(key) == 2 else (key[0] if key else None)
            elif key is False:  # Odoo's empty value
                key = None
            buckets[key] = buckets.get(key, 0) + 1
        return [{"key": k, "count": v} for k, v in buckets.items()]


def build_read_plane(client: SystemClient) -> ReadPlane:
    """Wire a ReadPlane over Odoo with an export store (PII-at-rest, tenant-scoped, TTL'd)."""
    export_dir = Path(os.environ.get("NIL_EXPORT_DIR", "/tmp/nil-exports"))
    return ReadPlane(OdooReadBackend(client), export_store=ExportStore(root=export_dir))
