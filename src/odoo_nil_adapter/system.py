"""System-client layer: the ONE module that performs I/O — here, against Odoo (XML-RPC External API).

`SystemClient` is the protocol the edge/translation depend on, so the conformance proof can run
against `FakeSystem` with no live instance. `RealSystemClient` speaks Odoo's `/xmlrpc/2` ORM
(`authenticate` + `execute_kw`: create / search_read / write / unlink / fields_get / read).

A NIL `target` IS an Odoo model name (e.g. `crm.lead`, `res.partner`, `crm.stage`). Credentials are
passed in by the runner from the environment — this module never reads or hardcodes them.
"""

from __future__ import annotations

import json as _json
import threading
import time
import uuid
import xmlrpc.client
from http.cookiejar import CookieJar
from typing import Any, Callable, Protocol
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener

# Transport statuses worth retrying — Odoo rate-limits (429) and transient gateway faults (5xx). An
# application Fault (bad args, access error) is NOT here: it is terminal and must surface immediately.
_RETRYABLE_HTTP: frozenset[int] = frozenset({429, 502, 503, 504})

# The portal document pages Odoo itself serves per model (portal.mixin records). Only targets listed
# here can be rendered via the portal route; an unlisted target REFUSES rather than guessing a URL.
_PORTAL_DOC_PATHS: dict[str, str] = {
    "purchase.order": "my/purchase",
    "account.move": "my/invoices",
}


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
        order: str | None = None,
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

    def render_report(  # the ERP's OWN rendered document (QWeb PDF) for one record — a pure READ
        self, report_ref: str, target: str, record_id: str
    ) -> bytes: ...


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

    def __init__(self, base_url: str, *, db: str, login: str, api_key: str,
                 max_retries: int = 3, backoff: float = 0.5, min_interval: float = 0.0,
                 http_timeout: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._url = base_url.rstrip("/")
        self._db = db
        self._login = login
        self._key = api_key
        self._uid: int | None = None
        self._version_major: int | None = None  # lazily probed via common.version()
        self._http_timeout = http_timeout       # the report controller is an HTTP GET, not XML-RPC
        self._fields_cache: dict[str, list[dict[str, Any]] | None] = {}
        # Durability (the adapter's slice — the "429 flood" lesson): retry transient transport faults
        # with exponential backoff, and optionally throttle to a minimum inter-call interval so a burst
        # of governed writes never hammers Odoo. Orchestration-level durability (resume across crashes,
        # per-tenant queues) is the separate Temporal plan; this keeps a SINGLE call resilient.
        self._max_retries = max_retries
        self._backoff = backoff
        self._min_interval = min_interval
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call: float = 0.0
        self._common = xmlrpc.client.ServerProxy(f"{self._url}/xmlrpc/2/common", allow_none=True)
        self._models = xmlrpc.client.ServerProxy(f"{self._url}/xmlrpc/2/object", allow_none=True)
        # G24: ServerProxy wraps ONE http.client connection — a state machine that two concurrent
        # calls corrupt ("CannotSendRequest: Request-sent" / "ResponseNotReady: Idle"; witnessed
        # live when a bulk fan-out committed two items at once and BOTH failed_terminal). The edge
        # serves sync endpoints from a threadpool and the tenant's client is cached, so the CLIENT
        # owns serialization. The lock guards ONLY the wire call, never a backoff sleep.
        self._xmlrpc_lock = threading.Lock()

    def _throttle(self) -> None:
        """Hold each upstream call to at least `min_interval` apart (off when 0) — a simple governor so
        a bulk of approved writes can't burst-trip Odoo's rate limiter."""
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (self._monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = self._monotonic()

    # ── auth + low-level call ────────────────────────────────────────────────────────────────
    def _auth(self) -> int:
        if self._uid:
            return self._uid
        try:
            with self._xmlrpc_lock:
                uid = self._common.authenticate(self._db, self._login, self._key, {})
        except Exception as exc:  # noqa: BLE001 — surface any transport/XML fault as a System refusal
            raise SystemError(f"odoo authenticate transport error: {exc}") from exc
        if not uid:
            raise SystemError("odoo authentication failed — check ODOO_DB / ODOO_LOGIN / ODOO_API_KEY")
        self._uid = int(uid)
        return self._uid

    def _kw(self, model: str, method: str, args: list[Any], kw: dict[str, Any] | None = None) -> Any:
        uid = self._auth()
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                with self._xmlrpc_lock:
                    return self._models.execute_kw(
                        self._db, uid, self._key, model, method, args, kw or {}
                    )
            except xmlrpc.client.Fault as fault:  # application error — terminal, never retried
                tail = fault.faultString.strip().splitlines()[-1] if fault.faultString else "fault"
                raise SystemError(f"odoo {model}.{method}: {tail}") from fault
            except xmlrpc.client.ProtocolError as pe:  # transport status — retry the rate-limit/gateway ones
                if pe.errcode in _RETRYABLE_HTTP and attempt < self._max_retries:
                    self._sleep(self._backoff * (2 ** attempt))
                    continue
                raise SystemError(f"odoo {model}.{method} transport error: HTTP {pe.errcode}") from pe
            except Exception as exc:  # noqa: BLE001 — other transient transport faults: retry, then surface
                if attempt < self._max_retries:
                    self._sleep(self._backoff * (2 ** attempt))
                    continue
                raise SystemError(f"odoo {model}.{method} transport error: {exc}") from exc
        raise SystemError(f"odoo {model}.{method}: exhausted {self._max_retries} retries")  # unreachable guard

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

    # ── the ERP's OWN rendered document (QWeb report → PDF bytes) ────────────────────────────
    # Creating a purchase order in Odoo returns the RECORD, never the document. The PDF the vendor is
    # meant to receive is rendered SEPARATELY, by Odoo's own QWeb report (purchase.report_purchaseorder,
    # account.report_invoice) — with Odoo's numbering, tax lines, terms, logo and legal footer. Two
    # routes exist to it, and WHICH one is open depends on the Odoo version:
    #
    #  • Odoo ≤ 14 — `ir.actions.report.render_qweb_pdf(res_ids)` is PUBLIC, so `execute_kw` may call it
    #    on the report record. It returns (pdf_bytes, "pdf"); XML-RPC marshals the bytes as base64 and
    #    xmlrpc.client hands them back as an `xmlrpc.client.Binary`.
    #  • Odoo ≥ 15 — the method was renamed `_render_qweb_pdf`, and Odoo's `execute_kw` REFUSES any
    #    method whose name begins with `_` (odoo.service.model.check_method_name → AccessError). So on
    #    15+ the private method is not reachable over XML-RPC AT ALL. Odoo's report controller over
    #    HTTP is one honest route there:
    #        POST /web/session/authenticate  (db + login + API key) → session cookie
    #        GET  /report/pdf/<report_ref>/<record_id>              → the exact bytes Odoo itself mails
    #  • Odoo SaaS (witnessed live on ahmedco2.odoo.com, saas~19.3, 2026-07-16) — the session route is
    #    ALSO closed: `/web/session/authenticate` answers AccessDenied to an API key (RPC-scoped keys
    #    open no web session). What IS open is Odoo's own portal document controller:
    #        GET /my/purchase/<id>?report_type=pdf&download=true&access_token=<token>
    #    token-gated, session-less, rendering the SAME QWeb report Odoo mails the vendor. The token is
    #    the record's own `access_token` (portal.mixin); when absent, Odoo mints a uuid4 lazily via
    #    `_portal_ensure_token` — `_render_via_portal` does exactly that and nothing more.
    #
    # We probe the server version, try the route that version exposes first, and fall back to the
    # others rather than guess. If none yields bytes we raise SystemError and the caller REFUSES: this
    # adapter never synthesizes a document, and "I could not fetch the official PO" must never look
    # like "here is the official PO".
    #
    # WITNESSED 2026-07-16 on ahmedco2.odoo.com (saas~19.3+e): xmlrpc route → method does not exist;
    # session route → AccessDenied; portal route → HTTP 200 application/pdf, 29 534 bytes, %PDF magic
    # (purchase.order/1, P00001). The portal route is the one modern Odoo SaaS actually serves.
    def _server_major(self) -> int:
        """Odoo's major version, or 0 when it cannot be probed (→ try both routes)."""
        if self._version_major is None:
            try:
                with self._xmlrpc_lock:
                    info = self._common.version() or {}
                parts = info.get("server_version_info") or []
                self._version_major = int(parts[0]) if parts else 0
            except Exception:  # noqa: BLE001 — an unprobeable version is not a failure, just unknown
                self._version_major = 0
        return self._version_major

    def _render_via_xmlrpc(self, report_ref: str, target: str, record_id: str) -> bytes:
        """Odoo ≤ 14: the public `render_qweb_pdf` on the `ir.actions.report` record."""
        rid = _as_int(record_id)
        if rid is None:
            raise SystemError(f"odoo report render needs a numeric record id, got {record_id!r}")
        rows = self._kw(
            "ir.actions.report", "search_read",
            [[["report_name", "=", report_ref]]], {"fields": ["id"], "limit": 1},
        )
        if not rows:
            raise SystemError(f"odoo has no report named {report_ref!r}")
        result = self._kw("ir.actions.report", "render_qweb_pdf", [[rows[0]["id"]], [rid]])
        return _pdf_bytes(result)

    def _render_via_http(self, report_ref: str, target: str, record_id: str) -> bytes:
        """Odoo ≥ 15 (and any version): the report controller, behind a real session."""
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        payload = _json.dumps({
            "jsonrpc": "2.0", "method": "call",
            "params": {"db": self._db, "login": self._login, "password": self._key},
        }).encode("utf-8")
        auth_req = Request(
            f"{self._url}/web/session/authenticate", data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener.open(auth_req, timeout=self._http_timeout) as resp:
                body = _json.loads(resp.read() or b"{}")
        except Exception as exc:  # noqa: BLE001 — transport fault → a refusable System error
            raise SystemError(f"odoo session authenticate transport error: {exc}") from exc
        if body.get("error") or not (body.get("result") or {}).get("uid"):
            raise SystemError("odoo session authenticate failed — the report controller needs a session")
        url = f"{self._url}/report/pdf/{quote(report_ref, safe='')}/{quote(str(record_id), safe='')}"
        try:
            with opener.open(Request(url), timeout=self._http_timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                data = resp.read()
        except Exception as exc:  # noqa: BLE001
            raise SystemError(f"odoo report controller {report_ref}: {exc}") from exc
        if not data or not data.startswith(b"%PDF"):
            # Odoo answers an un-rendered report with an HTML error page, HTTP 200. Bytes that are not
            # a PDF are NOT a document — attaching them would mail the vendor an error page.
            raise SystemError(
                f"odoo report controller {report_ref} returned {len(data)} bytes of "
                f"{content_type or 'unknown type'}, not a PDF"
            )
        return data

    def _render_via_portal(self, report_ref: str, target: str, record_id: str) -> bytes:
        """Odoo SaaS (API-key-only credentials): the record's OWN portal page as PDF, token-gated.

        This is the route saas~19 actually serves: `/my/<page>/<id>?report_type=pdf&access_token=…`
        renders the same QWeb report the session-gated controller would, with no session. The token
        is the record's `access_token`; when the record has none yet, we mint the uuid4 Odoo's own
        `_portal_ensure_token` would mint on first portal use — access-provisioning metadata, written
        once, never business data. An existing token is reused and nothing is written."""
        page = _PORTAL_DOC_PATHS.get(target)
        if page is None:
            raise SystemError(f"odoo has no portal document page for {target!r} — portal route closed")
        rid = _as_int(record_id)
        if rid is None:
            raise SystemError(f"odoo portal render needs a numeric record id, got {record_id!r}")
        rows = self._kw(target, "read", [[rid]], {"fields": ["access_token"]})
        if not rows:
            raise SystemError(f"no {target} with id {rid}")
        token = rows[0].get("access_token")
        if not token:
            token = str(uuid.uuid4())
            self._kw(target, "write", [[rid], {"access_token": token}])
        url = (f"{self._url}/{page}/{rid}?report_type=pdf&download=true"
               f"&access_token={quote(str(token), safe='')}")
        opener = build_opener()
        try:
            with opener.open(Request(url), timeout=self._http_timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                data = resp.read()
        except Exception as exc:  # noqa: BLE001 — transport fault → a refusable System error
            raise SystemError(f"odoo portal controller {page}/{rid}: {exc}") from exc
        if not data or not data.startswith(b"%PDF"):
            # A refused/expired token comes back as an HTML page, HTTP 200. Bytes that are not a PDF
            # are NOT a document — attaching them would mail the vendor an error page.
            raise SystemError(
                f"odoo portal controller {page}/{rid} returned {len(data)} bytes of "
                f"{content_type or 'unknown type'}, not a PDF"
            )
        return data

    def render_report(self, report_ref: str, target: str, record_id: str) -> bytes:
        major = self._server_major()
        routes: tuple[Callable[[str, str, str], bytes], ...] = (
            (self._render_via_http, self._render_via_portal, self._render_via_xmlrpc)
            if major == 0 or major >= 15
            else (self._render_via_xmlrpc, self._render_via_http, self._render_via_portal)
        )
        failures: list[str] = []
        for route in routes:
            try:
                pdf = route(report_ref, target, record_id)
            except SystemError as exc:
                failures.append(str(exc))
                continue
            if pdf:
                return pdf
            failures.append(f"{route.__name__} returned no bytes")
        raise SystemError(
            f"odoo could not render {report_ref} for {target}/{record_id}: " + " | ".join(failures)
        )


def _pdf_bytes(result: Any) -> bytes:
    """Unwrap what `render_qweb_pdf` returns over XML-RPC: (content, 'pdf'), where content arrives as
    an xmlrpc Binary (base64 on the wire), raw bytes, or a base64 string. Anything else is not a
    document and must raise rather than be attached to an email."""
    content = result[0] if isinstance(result, (list, tuple)) and result else result
    if isinstance(content, xmlrpc.client.Binary):
        return bytes(content.data)
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        import base64

        try:
            return base64.b64decode(content, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SystemError(f"odoo report render returned undecodable content: {exc}") from exc
    raise SystemError(f"odoo report render returned {type(content).__name__}, not document bytes")


class FakeSystem:
    """In-memory backend for the conformance proof — no live instance needed."""

    def __init__(self) -> None:
        self.docs: dict[str, list[dict[str, Any]]] = {}
        self.messages: dict[tuple[str, str], list[str]] = {}  # (target, record_id) -> chatter notes
        self.schemas: dict[str, list[dict[str, Any]]] = {}  # optional per-target field_meta (tests)
        self.method_calls: list[tuple[str, str, str, dict[str, Any]]] = []  # generic op=method invocations
        # The ERP's rendered documents, keyed (report_ref, record_id) — seeded by a test. The fake
        # STORES documents; it never renders one, because a document this adapter could compose is
        # exactly the thing the product must not send.
        self.reports: dict[tuple[str, str], bytes] = {}
        self.report_calls: list[tuple[str, str, str]] = []  # (report_ref, target, record_id)
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

    def render_report(self, report_ref: str, target: str, record_id: str) -> bytes:
        self.report_calls.append((report_ref, target, str(record_id)))
        try:
            return self.reports[(report_ref, str(record_id))]
        except KeyError:
            raise SystemError(
                f"fake: no rendered {report_ref} for {target}/{record_id}"
            ) from None
