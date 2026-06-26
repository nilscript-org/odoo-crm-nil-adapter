"""System-client layer: the ONE module that performs I/O — here, against Odoo (XML-RPC External API).

`SystemClient` is the protocol the edge/translation depend on, so the conformance proof can run
against `FakeSystem` with no live instance. `RealSystemClient` speaks Odoo's `/xmlrpc/2` ORM
(`authenticate` + `execute_kw`: create / search_read / write / unlink / fields_get / read).

A NIL `target` IS an Odoo model name (e.g. `crm.lead`, `res.partner`, `crm.stage`). Credentials are
passed in by the runner from the environment — this module never reads or hardcodes them.
"""

from __future__ import annotations

import xmlrpc.client
from typing import Any, Protocol


class SystemError(RuntimeError):
    """A write the System rejected — its message is surfaced/logged by the edge."""


class SystemClient(Protocol):
    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]: ...

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def search(  # indexed lookup by a native domain (phone/email match, dedup probe, tag listing)
        self,
        target: str,
        domain: list[list[Any]],
        *,
        fields: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]: ...

    def message_post(self, target: str, record_id: str, body: str) -> None: ...  # append a chatter note

    def call_method(  # invoke a governed workflow method on a record (action_post, button_validate, …)
        self, target: str, record_id: str, method: str, params: dict[str, Any]
    ) -> Any: ...

    def delete(self, target: str, record_id: str) -> None: ...

    def exists(self, target: str) -> bool: ...  # is this native target provisioned? (PROPOSE preflight)

    def count(self, target: str, domain: list[list[Any]]) -> int: ...  # O(1)-shaped 'how many'

    def schema(self, target: str) -> list[dict[str, Any]] | None: ...  # target shape (skeleton), or None

    def get(self, target: str, record_id: str) -> dict[str, Any] | None: ...  # one record (before-image)


def _triple(row: dict[str, Any], triple: list[Any]) -> bool:
    """Evaluate one Odoo domain triple [field, op, value] against an in-memory row (FakeSystem). Mirrors
    the op set the ReadPlane forwards, so the fake honours the same filters Odoo would server-side."""
    field, op, value = triple
    v = row.get(field)
    if op in ("=", "=="):
        return v == value or str(v if v is not None else "") == str(value)
    if op == "!=":
        return v != value
    if op in ("ilike", "like"):
        return str(value).lower() in str(v if v is not None else "").lower()
    if op == ">":
        return v is not None and v > value
    if op == ">=":
        return v is not None and v >= value
    if op == "<":
        return v is not None and v < value
    if op == "<=":
        return v is not None and v <= value
    if op == "in":
        return v in (value or [])
    return False


def _as_int(value: Any) -> int | None:
    """Odoo record ids are integers. Returns the int, or None for anything non-numeric (so a human
    identifier like an email is cleanly handled by the edge's id-or-identifier resolution)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _writable(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten an Odoo `read()` record into values safe to `write()`/`create()` back (used for the
    generic-CRUD before-image reversal): many2one [id, label] -> id; drop x2many lists, `id`, and
    audit fields. Relational restore is therefore best-effort (COMPENSABLE, not exact REVERSIBLE)."""
    drop = {"id", "create_uid", "create_date", "write_uid", "write_date", "__last_update"}
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key in drop:
            continue
        if isinstance(value, list):
            # many2one comes back as [id, "label"]; x2many as [id, id, ...] — only the former is safe.
            if len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], str):
                out[key] = value[0]
            continue
        out[key] = value
    return out


def _field_meta(name: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Resolution-oriented metadata for one field — what the edge resolver needs to know HOW to write
    it. Beyond name/type: a `selection` option list (enum — the value must be one of these keys), a
    `relation` (the comodel a many2one points to — the value must be a record id there), and the
    `readonly` flag (never a write target). This is the per-field surface that drives buckets A–F."""
    field: dict[str, Any] = {
        "name": name,
        "type": meta.get("type"),
        "required": bool(meta.get("required")),
        "readonly": bool(meta.get("readonly")),
    }
    selection = meta.get("selection")
    if isinstance(selection, (list, tuple)):  # Odoo gives [[value, label], …]
        options = [
            {"value": pair[0], "label": pair[1]}
            for pair in selection
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        ]
        if options:
            field["options"] = options
    if meta.get("relation"):
        field["relation"] = meta["relation"]
    return field


class RealSystemClient:
    """Talk to Odoo via the XML-RPC External API — the only I/O in the adapter."""

    def __init__(self, base_url: str, *, db: str, login: str, api_key: str) -> None:
        self._url = base_url.rstrip("/")
        self._db = db
        self._login = login
        self._key = api_key
        self._uid: int | None = None
        self._fields_cache: dict[str, list[dict[str, Any]] | None] = {}
        self._common = xmlrpc.client.ServerProxy(f"{self._url}/xmlrpc/2/common", allow_none=True)
        self._models = xmlrpc.client.ServerProxy(f"{self._url}/xmlrpc/2/object", allow_none=True)

    # ── auth + low-level call ────────────────────────────────────────────────────────────────
    def _auth(self) -> int:
        if self._uid:
            return self._uid
        try:
            uid = self._common.authenticate(self._db, self._login, self._key, {})
        except Exception as exc:  # noqa: BLE001 — surface any transport/XML fault as a System refusal
            raise SystemError(f"odoo authenticate transport error: {exc}") from exc
        if not uid:
            raise SystemError("odoo authentication failed — check ODOO_DB / ODOO_LOGIN / ODOO_API_KEY")
        self._uid = int(uid)
        return self._uid

    def _kw(self, model: str, method: str, args: list[Any], kw: dict[str, Any] | None = None) -> Any:
        uid = self._auth()
        try:
            return self._models.execute_kw(self._db, uid, self._key, model, method, args, kw or {})
        except xmlrpc.client.Fault as fault:
            tail = fault.faultString.strip().splitlines()[-1] if fault.faultString else "fault"
            raise SystemError(f"odoo {model}.{method}: {tail}") from fault
        except Exception as exc:  # noqa: BLE001
            raise SystemError(f"odoo {model}.{method} transport error: {exc}") from exc

    def _clean(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Strip keys that must not be written: `id` and any None-valued field."""
        return {k: v for k, v in doc.items() if k != "id" and v is not None}

    # ── SystemClient surface ─────────────────────────────────────────────────────────────────
    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        new_id = self._kw(target, "create", [self._clean(doc)])
        return self.get(target, str(new_id)) or {"id": new_id, **doc}

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        domain: list[Any] = []
        for field, value in (filters or {}).items():
            domain.append([field, "ilike", value] if isinstance(value, str) else [field, "=", value])
        rows = self._kw(target, "search_read", [domain], {"limit": 50})
        return [dict(r) for r in rows]

    def search(
        self,
        target: str,
        domain: list[list[Any]],
        *,
        fields: tuple[str, ...] | None = None,
        limit: int = 50,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        kw: dict[str, Any] = {"limit": limit}
        if fields:
            kw["fields"] = list(fields)
        if order:
            kw["order"] = order
        rows = self._kw(target, "search_read", [domain], kw)
        return [dict(r) for r in rows]

    def count(self, target: str, domain: list[list[Any]]) -> int:
        """O(1)-shaped 'how many' via Odoo `search_count` — never a full list to count."""
        return int(self._kw(target, "search_count", [domain]))

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        rid = _as_int(record_id)
        if rid is None:
            raise SystemError(f"odoo update needs a numeric record id, got {record_id!r}")
        self._kw(target, "write", [[rid], self._clean(doc)])
        return self.get(target, str(rid)) or {"id": rid}

    def message_post(self, target: str, record_id: str, body: str) -> None:
        rid = _as_int(record_id)
        if rid is None:
            raise SystemError(f"odoo message_post needs a numeric record id, got {record_id!r}")
        self._kw(target, "message_post", [[rid]], {"body": body})

    def call_method(self, target: str, record_id: str, method: str, params: dict[str, Any]) -> Any:
        """Invoke an arbitrary Odoo model method on one record via execute_kw (the workflow surface:
        action_post / button_validate / action_confirm / …). The edge only ever reaches here AFTER the
        governance allow-list grants (target, method) — this layer performs the call, never authorizes."""
        rid = _as_int(record_id)
        if rid is None:
            raise SystemError(f"odoo {method} needs a numeric record id, got {record_id!r}")
        return self._kw(target, method, [[rid]], dict(params or {}))

    def delete(self, target: str, record_id: str) -> None:
        rid = _as_int(record_id)
        if rid is None:
            raise SystemError(f"odoo delete needs a numeric record id, got {record_id!r}")
        self._kw(target, "unlink", [[rid]])

    def exists(self, target: str) -> bool:
        return self.schema(target) is not None

    def schema(self, target: str) -> list[dict[str, Any]] | None:
        if target in self._fields_cache:
            return self._fields_cache[target]
        try:
            fg = self._kw(target, "fields_get", [],
                          {"attributes": ["string", "type", "required", "readonly", "selection", "relation"]})
        except SystemError:
            self._fields_cache[target] = None  # model not provisioned / not accessible
            return None
        self._fields_cache[target] = [_field_meta(name, meta) for name, meta in sorted(fg.items())]
        return self._fields_cache[target]

    def get(self, target: str, record_id: str) -> dict[str, Any] | None:
        rid = _as_int(record_id)
        if rid is None:
            return None
        rows = self._kw(target, "read", [[rid]], {})
        return (_writable(rows[0]) | {"id": rid}) if rows else None


class FakeSystem:
    """In-memory backend for the conformance proof — no live instance needed."""

    def __init__(self) -> None:
        self.docs: dict[str, list[dict[str, Any]]] = {}
        self.messages: dict[tuple[str, str], list[str]] = {}  # (target, record_id) -> chatter notes
        self.schemas: dict[str, list[dict[str, Any]]] = {}  # optional per-target field_meta (tests)
        self.method_calls: list[tuple[str, str, str, dict[str, Any]]] = []  # generic op=method invocations
        self._counter = 0

    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        self._counter += 1
        name = str(doc.get("name") or f"{target}-{self._counter:05d}")
        record = {**doc, "name": name, "target": target}
        self.docs.setdefault(target, []).append(record)
        return record

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rows = list(self.docs.get(target, []))
        for field, value in (filters or {}).items():
            rows = [r for r in rows if str(value).lower() in str(r.get(field, "")).lower()]
        return rows

    def search(
        self,
        target: str,
        domain: list[list[Any]],
        *,
        fields: tuple[str, ...] | None = None,
        limit: int = 50,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        # Interpret the same AND-of-triples domain RealSystemClient forwards to Odoo search_read.
        rows = [r for r in self.docs.get(target, []) if all(_triple(r, t) for t in (domain or []))]
        if order:
            key = order.split()[0]
            rows = sorted(rows, key=lambda r: (r.get(key) is None, r.get(key)), reverse="desc" in order)
        return rows[:limit]

    def count(self, target: str, domain: list[list[Any]]) -> int:
        return sum(1 for r in self.docs.get(target, []) if all(_triple(r, t) for t in (domain or [])))

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        for record in self.docs.get(target, []):
            if record.get("name") == record_id:
                record.update(doc)
                return record
        record = {**doc, "name": record_id, "target": target}  # upsert keeps the proof deterministic
        self.docs.setdefault(target, []).append(record)
        return record

    def message_post(self, target: str, record_id: str, body: str) -> None:
        self.messages.setdefault((target, record_id), []).append(body)

    def call_method(self, target: str, record_id: str, method: str, params: dict[str, Any]) -> Any:
        self.method_calls.append((target, record_id, method, dict(params or {})))
        if method == "message_post":  # keep chatter semantics so the note surface stays observable
            self.message_post(target, record_id, str((params or {}).get("body", "")))
        return True

    def delete(self, target: str, record_id: str) -> None:
        self.docs[target] = [r for r in self.docs.get(target, []) if r.get("name") != record_id]

    def exists(self, target: str) -> bool:
        return True  # in-memory backend is always ready (creates targets on demand)

    def schema(self, target: str) -> list[dict[str, Any]] | None:
        return self.schemas.get(target, [])  # seeded field_meta if a test set it, else provisioned/empty

    def get(self, target: str, record_id: str) -> dict[str, Any] | None:
        return next((r for r in self.docs.get(target, []) if r.get("name") == record_id), None)
