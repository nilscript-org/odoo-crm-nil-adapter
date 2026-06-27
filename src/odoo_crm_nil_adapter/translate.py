"""The translation core: NIL verb args ⇄ Odoo native documents. Pure mapping, no I/O.

This is the only module (besides system.py) that knows Odoo specifics. A NIL `target`/`doctype` is
an Odoo model name (`crm.lead`, `res.partner`). Two surfaces ship:

  • Generic `resource.*` CRUD — handled directly by the edge against ANY Odoo model, with
    synthesized reversibility (create→delete, update→restore-before-image). No authoring here.
  • Semantic `crm.*` verbs below — curated args, bilingual previews, and tiers for the common CRM
    flows (create lead / contact, move stage, delete). Reversibility is declared in compensation.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from odoo_crm_nil_adapter.system import SystemClient, SystemError

Bilingual = dict[str, str]


# The committable target set for the generic `resource.*` CRUD family — the adapter's DECLARED
# skeleton for direct CRUD. `describe()` advertises exactly this set, and the edge refuses
# `resource.*` against any target outside it (default-deny). So advertised ≡ committable: a CRM
# adapter wired to a full Odoo cannot be steered into accounting/HR/payroll (account.payment,
# hr.employee, …). Scoped to the CRM domain the curated verbs + reference resolvers legitimately
# use; widen DELIBERATELY (and re-advertise) only when a real flow needs another model.
DECLARED_TARGETS: frozenset[str] = frozenset({
    "crm.lead",             # opportunities / leads
    "res.partner",          # contacts / customers
    "crm.stage",            # pipeline stages
    "crm.tag",              # opportunity tags
    "res.partner.category", # contact tags
    "crm.team",             # sales teams (assignment)
    "res.country",          # country reference (country_id resolution)
    "res.country.state",    # state / region reference
})

RESOURCE_VERBS: tuple[str, ...] = (
    "resource.create", "resource.read", "resource.update", "resource.delete",
    "resource.method",  # generic governed workflow-method invocation (action_post, button_validate, …)
)


@dataclass(frozen=True)
class WriteVerb:
    verb: str
    tier: str
    doctype: str  # the Odoo model this verb writes (e.g. "crm.lead", "res.partner")
    # Explicit execution strategy. The edge dispatches COMMIT on THIS, never on the verb name —
    # name-prefix inference only ever modelled CRUD. `op` is the spine every verb plugs into.
    op: str  # one of: "create" | "update" | "delete"  (later: "archive" | "upsert" | "method")
    required: tuple[str, ...]
    to_native: Callable[[dict[str, Any]], dict[str, Any]]
    preview: Callable[[dict[str, Any]], Bilingual]
    entity_type: str
    # for op="upsert": native fields probed (in order) to find an existing record before writing —
    # so an at-least-once webhook retry updates the identity instead of duplicating it (the moat).
    dedup_keys: tuple[str, ...] = ()
    method: str | None = None  # for op="method": the Odoo model method to invoke (e.g. "message_post")
    # NIL arg keys this verb can actually write/use. When declared, PROPOSE flags any provided arg
    # outside this set as `ignored` — so an unwritable field (e.g. country) is never silently accepted.
    supported_args: tuple[str, ...] = ()
    # many2one (dropdown) args this verb resolves to a backend reference id before writing. Each entry
    # is (nil_arg, native_field, model): the edge looks the human value up in `model` and writes the
    # resolved id into `native_field` — never the raw string (a relational field rejects text). The
    # supported_args list still gates acceptance; references only declares HOW the value is resolved.
    references: tuple[tuple[str, str, str], ...] = ()

    def missing(self, args: dict[str, Any]) -> list[str]:
        return [field for field in self.required if not args.get(field)]


@dataclass(frozen=True)
class QueryVerb:
    verb: str
    run: Callable[[SystemClient, dict[str, Any]], dict[str, Any]]


def _maybe_int(value: Any) -> Any:
    """Coerce to int when it looks numeric, else pass through unchanged (keeps to_native pure and
    crash-free on placeholder/conformance args while sending Odoo a real int when given one)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def _maybe_float(value: Any) -> Any:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return value


# ── crm.* → Odoo field maps (pure; never raise on odd input) ─────────────────────────────────
def _to_native_create_lead(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → an Odoo `crm.lead` (an opportunity in the CRM pipeline)."""
    doc: dict[str, Any] = {"name": args["name"], "type": args.get("lead_type", "opportunity")}
    for nil_key, odoo_key in (
        ("contact_name", "contact_name"),
        ("email", "email_from"),
        ("phone", "phone"),
        ("description", "description"),
    ):
        if args.get(nil_key):
            doc[odoo_key] = args[nil_key]
    if args.get("expected_revenue") is not None:
        doc["expected_revenue"] = _maybe_float(args["expected_revenue"])
    if args.get("partner_id"):
        doc["partner_id"] = _maybe_int(args["partner_id"])
    return doc


def _to_native_create_contact(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → an Odoo `res.partner` (a CRM contact / customer)."""
    doc: dict[str, Any] = {"name": args["name"]}
    for nil_key, odoo_key in (("email", "email"), ("phone", "phone"), ("company", "company_name")):
        if args.get(nil_key):
            doc[odoo_key] = args[nil_key]
    if args.get("is_company") is not None:
        doc["is_company"] = bool(args["is_company"])
    return doc


# res.partner fields a curated contact update may touch. The whitelist IS this map — `to_native`
# never blind-writes args, so a stray field (credit_limit, system flags) can't reach Odoo.
_CONTACT_WRITABLE: tuple[tuple[str, str], ...] = (
    ("name", "name"),
    ("phone", "phone"),
    ("email", "email"),
    ("comment", "comment"),
    ("company", "company_name"),
)


def _to_native_update_contact(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → a whitelisted `res.partner` patch. `contact_id` is the record id (used by the edge),
    not a written field. Only declared contact fields pass; everything else is dropped."""
    doc: dict[str, Any] = {}
    for nil_key, odoo_key in _CONTACT_WRITABLE:
        if args.get(nil_key) is not None:
            doc[odoo_key] = args[nil_key]
    return doc


def _to_native_update_lead_stage(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → the `crm.lead` patch that moves it to another pipeline stage. `lead_id` is the
    record id (used by the edge), not a written field — only `stage_id` is patched."""
    return {"stage_id": _maybe_int(args.get("stage_id"))}


def _to_native_delete(_args: dict[str, Any]) -> dict[str, Any]:
    """delete_* verbs identify the record by their first required arg; nothing is written."""
    return {}


def _to_native_log_note(args: dict[str, Any]) -> dict[str, Any]:
    """log_note carries only the chatter `body`; `contact_id` is the record id (used by the edge).
    `.get` (not `[]`) keeps to_native crash-free if a malformed COMMIT bypasses PROPOSE validation."""
    return {"body": args.get("body", "")}


# ── semantic verbs (Phase 6): curated sugar over the generic plane for the highest-value flows.
# Capability comes from the universal plane (resource.* CRUD + resource.method); these add clean args,
# bilingual approval previews, and a precise per-verb tier. Representative set across module groups —
# the long tail stays on the generic plane (the "don't hand-write a thousand verbs" discipline).
def _to_native_create_invoice(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → an Odoo customer invoice (`account.move`, move_type=out_invoice). Lines are the Odoo
    one2many write form [(0, 0, {…})]; the hidden move_type is supplied so the agent never sees it."""
    doc: dict[str, Any] = {"move_type": "out_invoice"}
    if args.get("partner_id"):
        doc["partner_id"] = _maybe_int(args["partner_id"])
    if args.get("invoice_date"):
        doc["invoice_date"] = args["invoice_date"]
    lines = args.get("lines") or []
    if lines:
        doc["invoice_line_ids"] = [
            (0, 0, {"name": ln.get("name", ""), "quantity": _maybe_float(ln.get("quantity", 1)),
                    "price_unit": _maybe_float(ln.get("price_unit", 0))})
            for ln in lines if isinstance(ln, dict)
        ]
    return doc


def _to_native_method_only(_args: dict[str, Any]) -> dict[str, Any]:
    """A workflow-method verb (validate / confirm) writes no fields — the record id is the first
    required arg, consumed by the edge; the method itself drives the state transition in Odoo."""
    return {}


# ── crm.* read-through verbs (fresh business truth, no side effects) ──────────────────────────
def _run_list_leads(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    rows = client.list("crm.lead", args.get("match") or None)
    return {"target": "crm.lead", "count": len(rows), "items": rows}


def _run_list_contacts(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    rows = client.list("res.partner", args.get("match") or None)
    return {"target": "res.partner", "count": len(rows), "items": rows}


def _run_list_stages(client: SystemClient, _args: dict[str, Any]) -> dict[str, Any]:
    rows = client.list("crm.stage", None)
    return {"target": "crm.stage", "count": len(rows), "items": rows}


def _run_list_countries(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    """Read the country dropdown (res.country) so a caller can see the values country_id accepts —
    optionally filtered by a name/code fragment (`match`). This is the read surface behind the
    reference resolver: the value the agent picks here is what update_contact's `country` resolves."""
    match = str(args.get("match", "")).strip()
    domain = ["|", ["name", "ilike", match], ["code", "ilike", match]] if match else []
    rows = client.search("res.country", domain, fields=("id", "name", "code"), limit=300)
    return {"target": "res.country", "count": len(rows), "items": rows}


def _run_get_contact_by_phone(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    """Hot-path identity lookup: the WhatsApp entry point IS a phone number. Indexed exact match on
    `phone`, falling back to Odoo's normalized `phone_sanitized` (handles formatting differences) —
    never a full-scan via list_contacts. An unmatched number is a valid empty read, not an error."""
    phone = str(args.get("phone", "")).strip()
    if not phone:
        return {"target": "res.partner", "count": 0, "items": []}
    rows = _safe_search(client, [["phone", "=", phone]])
    if not rows:  # the stored value may be formatted differently than the inbound number
        rows = _safe_search(client, [["phone_sanitized", "=", phone]])
    return {"target": "res.partner", "count": len(rows), "items": rows}


def _safe_search(client: SystemClient, domain: list[list[Any]]) -> list[dict[str, Any]]:
    """A read-side search that degrades to empty if the backend rejects the domain (e.g. a field the
    model lacks) — a lookup must never turn a missing match into a 500."""
    try:
        return client.search("res.partner", domain, limit=10)
    except SystemError:
        return []


WRITE_VERBS: dict[str, WriteVerb] = {
    "crm.create_lead": WriteVerb(
        verb="crm.create_lead",
        tier="MEDIUM",
        doctype="crm.lead",
        op="create",
        required=("name",),
        to_native=_to_native_create_lead,
        preview=lambda a: {
            "en": f"Create CRM opportunity “{a.get('name', '')}”"
            + (f" for {a['contact_name']}" if a.get("contact_name") else ""),
            "ar": f"إنشاء فرصة بيعية «{a.get('name', '')}»"
            + (f" للعميل {a['contact_name']}" if a.get("contact_name") else ""),
        },
        entity_type="lead",
    ),
    "crm.create_contact": WriteVerb(
        verb="crm.create_contact",
        tier="MEDIUM",
        doctype="res.partner",
        op="upsert",  # create-or-update: dedup on email/phone so retries don't fork the identity graph
        required=("name",),
        to_native=_to_native_create_contact,
        preview=lambda a: {
            "en": f"Create or update contact “{a.get('name', '')}”"
            + (f" <{a['email']}>" if a.get("email") else ""),
            "ar": f"إنشاء أو تحديث جهة اتصال «{a.get('name', '')}»"
            + (f" <{a['email']}>" if a.get("email") else ""),
        },
        entity_type="contact",
        dedup_keys=("email", "phone"),
    ),
    "crm.update_contact": WriteVerb(
        verb="crm.update_contact",
        tier="MEDIUM",
        doctype="res.partner",
        op="update",
        required=("contact_id",),
        to_native=_to_native_update_contact,
        preview=lambda a: {
            "en": f"Update contact {a.get('contact_id', '')}"
            + (f" → {a['email']}" if a.get("email") else ""),
            "ar": f"تحديث جهة الاتصال {a.get('contact_id', '')}"
            + (f" ← {a['email']}" if a.get("email") else ""),
        },
        entity_type="contact",
        supported_args=("contact_id", "name", "phone", "email", "comment", "company", "country"),
        # `country` is a dropdown (res.partner.country_id is many2one → res.country): accept the human
        # name ("قطر"/"Qatar"/"QA"), resolve it to the country record id, write country_id. Writing the
        # raw string would be rejected by the relational field — resolution is the whole point.
        references=(("country", "country_id", "res.country"),),
    ),
    "crm.log_note": WriteVerb(
        verb="crm.log_note",
        tier="MEDIUM",
        doctype="res.partner",
        op="method",
        method="message_post",
        required=("contact_id", "body"),
        to_native=_to_native_log_note,
        preview=lambda a: {
            "en": f"Log a note on contact {a.get('contact_id', '')}: “{a.get('body', '')}”",
            "ar": f"تسجيل ملاحظة على جهة الاتصال {a.get('contact_id', '')}: «{a.get('body', '')}»",
        },
        entity_type="contact",
    ),
    "crm.update_lead_stage": WriteVerb(
        verb="crm.update_lead_stage",
        tier="MEDIUM",
        doctype="crm.lead",
        op="update",
        required=("lead_id", "stage_id"),
        to_native=_to_native_update_lead_stage,
        preview=lambda a: {
            "en": f"Move lead {a.get('lead_id', '')} to stage {a.get('stage_id', '')}",
            "ar": f"نقل الفرصة {a.get('lead_id', '')} إلى المرحلة {a.get('stage_id', '')}",
        },
        entity_type="lead",
    ),
    "crm.delete_lead": WriteVerb(
        verb="crm.delete_lead",
        tier="HIGH",
        doctype="crm.lead",
        op="delete",
        required=("lead_id",),
        to_native=_to_native_delete,
        preview=lambda a: {
            "en": f"Delete CRM lead {a.get('lead_id', '')}",
            "ar": f"حذف الفرصة البيعية {a.get('lead_id', '')}",
        },
        entity_type="lead",
    ),
    "crm.delete_contact": WriteVerb(
        verb="crm.delete_contact",
        tier="HIGH",
        doctype="res.partner",
        op="delete",
        required=("contact_id",),
        to_native=_to_native_delete,
        preview=lambda a: {
            "en": f"Delete contact {a.get('contact_id', '')}",
            "ar": f"حذف جهة الاتصال {a.get('contact_id', '')}",
        },
        entity_type="contact",
    ),
    # ── semantic verbs across module groups (Phase 6) ─────────────────────────────────────────────
    "account.create_invoice": WriteVerb(
        verb="account.create_invoice",
        tier="HIGH",  # a financial document — owner-reviewed, never MEDIUM-auto like a CRM note
        doctype="account.move",
        op="create",
        required=("partner_id",),
        to_native=_to_native_create_invoice,
        preview=lambda a: {
            "en": f"Create a customer invoice for partner {a.get('partner_id', '')}"
            + (f" ({len(a['lines'])} line(s))" if a.get("lines") else ""),
            "ar": f"إنشاء فاتورة عميل للعميل {a.get('partner_id', '')}"
            + (f" ({len(a['lines'])} بند)" if a.get("lines") else ""),
        },
        entity_type="invoice",
        supported_args=("partner_id", "invoice_date", "lines"),
    ),
    "stock.validate_picking": WriteVerb(
        verb="stock.validate_picking",
        tier="HIGH",
        doctype="stock.picking",
        op="method",
        method="button_validate",
        required=("picking_id",),
        to_native=_to_native_method_only,
        preview=lambda a: {
            "en": f"Validate stock transfer {a.get('picking_id', '')} (commit the moves)",
            "ar": f"اعتماد إذن الصرف {a.get('picking_id', '')} (تثبيت الحركات)",
        },
        entity_type="picking",
    ),
    "sale.confirm_order": WriteVerb(
        verb="sale.confirm_order",
        tier="HIGH",
        doctype="sale.order",
        op="method",
        method="action_confirm",
        required=("order_id",),
        to_native=_to_native_method_only,
        preview=lambda a: {
            "en": f"Confirm sales order {a.get('order_id', '')}",
            "ar": f"تأكيد أمر البيع {a.get('order_id', '')}",
        },
        entity_type="sale_order",
    ),
}


QUERY_VERBS: dict[str, QueryVerb] = {
    "crm.list_leads": QueryVerb(verb="crm.list_leads", run=_run_list_leads),
    "crm.list_contacts": QueryVerb(verb="crm.list_contacts", run=_run_list_contacts),
    "crm.list_stages": QueryVerb(verb="crm.list_stages", run=_run_list_stages),
    "crm.list_countries": QueryVerb(verb="crm.list_countries", run=_run_list_countries),
    "crm.get_contact_by_phone": QueryVerb(verb="crm.get_contact_by_phone", run=_run_get_contact_by_phone),
}


# ── the universal read data plane (nil.*): lean, filtered, paginated, governed ────────────────────
# These delegate to the shared `ReadPlane` (projection + byte-cap-refuse + capability fallback + read
# authz + export/bulk gating). The edge dispatches them through QUERY_VERBS like any read verb; engine
# refusals come back as structured `{outcome: refused, code, message}` answers, never 500s.
from datetime import UTC, datetime  # noqa: E402

from nilscript.dataplane import (  # noqa: E402
    Binding,
    BulkApprovalRequired,
    CapabilityUnsupported,
    IdentityResolver,
    Intent,
    IntentResolver,
    InvalidFilter,
    ResultTooLarge,
)

from odoo_crm_nil_adapter.read_plane import build_read_plane  # noqa: E402

import weakref  # noqa: E402

_READ_REFUSALS = (ResultTooLarge, InvalidFilter, CapabilityUnsupported, BulkApprovalRequired)
# Keyed by the client OBJECT (WeakKeyDictionary), not id(client): id() is reused after GC, which would
# hand a fresh client a stale plane bound to a dead backend. Weak keys are GC-safe and collision-free.
_PLANES: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()


def _plane(client: SystemClient) -> Any:
    plane = _PLANES.get(client)
    if plane is None:
        plane = build_read_plane(client)
        _PLANES[client] = plane
    return plane


def _refusal(exc: Exception) -> dict[str, Any]:
    return {"outcome": "refused", "code": getattr(exc, "code", "ERROR"),
            "message": getattr(exc, "message", str(exc))}


# Default to an EMPTY grant (`()`), so the ReadPlane redacts every field classified sensitive
# (salary / VAT / IBAN / credit on financial & HR models) unless the caller explicitly `reveal`s it.
# `None` would mean "unrestricted" and leak those by default — discovery must never do that.
def _grant(args: dict[str, Any]) -> tuple[str, ...]:
    reveal = args.get("reveal")
    return tuple(reveal) if reveal else ()


def _run_nil_search(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return _plane(client).search(
            args["target"], filter=args.get("filter") or [], fields=args.get("fields"),
            limit=int(args.get("limit") or 50), cursor=args.get("cursor"), grant_fields=_grant(args),
        )
    except _READ_REFUSALS as exc:
        return _refusal(exc)


def _run_nil_count(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return _plane(client).count(args["target"], filter=args.get("filter") or [])
    except _READ_REFUSALS as exc:
        return _refusal(exc)


def _run_nil_get(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    try:
        rec = _plane(client).get(args["target"], record_id=args.get("id"), fields=args.get("fields"),
                                 grant_fields=_grant(args))
        return rec if rec is not None else {"found": False, "id": args.get("id")}
    except _READ_REFUSALS as exc:
        return _refusal(exc)


def _run_nil_aggregate(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return _plane(client).aggregate(
            args["target"], filter=args.get("filter") or [],
            group_by=args["group_by"], metrics=tuple(args.get("metrics") or ("count",)),
        )
    except _READ_REFUSALS as exc:
        return _refusal(exc)


def _run_nil_export(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    try:
        handle = _plane(client).export(
            args["target"], filter=args.get("filter") or [], fields=args.get("fields"),
            tenant=str(args.get("tenant") or "default"), now=datetime.now(UTC),
            approved=bool(args.get("approved")),
        )
        return {"handle": handle.handle, "format": handle.format, "rows": handle.rows,
                "bytes": handle.bytes, "schema": handle.schema, "expires_at": handle.expires_at.isoformat()}
    except _READ_REFUSALS as exc:
        return _refusal(exc)


_RESOLVERS: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()


def _resolver(client: SystemClient) -> Any:
    r = _RESOLVERS.get(client)
    if r is None:
        r = IntentResolver(_plane(client), IdentityResolver())
        _RESOLVERS[client] = r
    return r


def _run_nil_intent(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    """The single intent payload: build an Intent and resolve it deterministically over the ReadPlane.
    The caller selects no verb and builds no filter — the system owns the mechanics."""
    where = tuple(
        Binding(attr=b.get("attr"), rel=b.get("rel"), value=b.get("value"))
        for b in (args.get("where") or [])
    )
    intent = Intent(about=args.get("about", ""), where=where, seek=args.get("seek", "all"),
                    by=args.get("by"), limit=int(args.get("limit") or 50), cursor=args.get("cursor"))
    try:
        outcome = _resolver(client).resolve(intent)
    except SystemError as exc:  # an upstream (Odoo) fault is a structured refusal, never a 500
        return {"outcome": "refused", "code": "UPSTREAM_ERROR", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — any resolution fault is a structured refusal, never a 500
        return {"outcome": "refused", "code": "INTENT_ERROR", "message": str(exc)}
    if outcome.kind == "refusal":
        return {"outcome": "refused", "code": outcome.code, "message": outcome.fix}
    return {"outcome": "result", "value": outcome.value}


QUERY_VERBS.update({
    "nil.intent": QueryVerb(verb="nil.intent", run=_run_nil_intent),
    "nil.search": QueryVerb(verb="nil.search", run=_run_nil_search),
    "nil.count": QueryVerb(verb="nil.count", run=_run_nil_count),
    "nil.get": QueryVerb(verb="nil.get", run=_run_nil_get),
    "nil.aggregate": QueryVerb(verb="nil.aggregate", run=_run_nil_aggregate),
    "nil.export": QueryVerb(verb="nil.export", run=_run_nil_export),
})


def entity_ref(verb: WriteVerb, created: dict[str, Any]) -> dict[str, Any]:
    # The SSOT entity id MUST be the backend's real record key, so a compensating delete (ROLLBACK)
    # targets the record itself — never a human attribute that can collide or change.
    rid = created.get("id") or created.get("name") or ""
    slug = verb.doctype.replace(".", "-")
    return {"type": verb.entity_type, "id": rid, "url": f"/{slug}/{rid}"}
