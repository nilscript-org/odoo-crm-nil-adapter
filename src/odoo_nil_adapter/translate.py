"""The translation core: NIL verb args ⇄ Odoo native documents. Pure mapping, no I/O.

This is the only module (besides system.py) that knows Odoo specifics. A NIL `target`/`doctype` is
an Odoo model name (`crm.lead`, `res.partner`). Two surfaces ship:

  • Generic `resource.*` CRUD — handled directly by the edge against ANY Odoo model, with
    synthesized reversibility (create→delete, update→restore-before-image). No authoring here.
  • Semantic `crm.*` verbs below — curated args, bilingual previews, and tiers for the common CRM
    flows (create lead / contact, move stage, delete). Reversibility is declared in compensation.py.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable

from odoo_nil_adapter.system import SystemClient, SystemError

Bilingual = dict[str, str]


# DECLARED_TARGETS, WRITE_VERBS, QUERY_VERBS are computed at module bottom from packs.
# They are declared here so static analysis finds the names; actual values assigned at end of file.

RESOURCE_VERBS: tuple[str, ...] = (
    "resource.create",
    "resource.read",
    "resource.update",
    "resource.delete",
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
    # Each entry is one of:
    #   - a single field name (OR semantics: that ONE field's value alone identifies the record);
    #   - a `tuple[str, ...]` of field names — a COMPOUND key, probed as one AND-of-EQUALITIES domain
    #     against whatever `to_native` wrote (edge.py's op=upsert dispatch, generalized fix round 1)
    #     — for an identity no single field can carry on its own (e.g. a link keyed on
    #     (parent_id, child_id));
    #   - a compound group of fully-resolved domain TRIPLES, `tuple[tuple[str, str, Any], ...]` — e.g.
    #     `(("email", "=", "a@b.c"), ("supplier_rank", ">", 0))` (I4 re-review, fix round 2) — for an
    #     identity that needs a RANGE or other non-equality predicate the plain field-name shape
    #     cannot express at all (equality-only against `native`). Every value here is already resolved
    #     by the verb's own `dedup_probe`, never sourced from `native`. `edge.py`'s `_is_domain_group`
    #     tells the two compound shapes apart by TUPLE SHAPE alone — still vendor-neutral.
    dedup_keys: tuple[str | tuple[str, ...] | tuple[tuple[str, str, Any], ...], ...] = ()
    # Fix round 1 (Task 1.3b, D-concern-1): `dedup_keys`' OR semantics tries EVERY declared entry in
    # order and stops at the first hit — which is right when the keys are alternative spellings of
    # the SAME identity (crm.create_contact's email-or-phone: whichever the caller happened to give
    # names the same contact). It is WRONG when the keys are TIERS of a fallback — "probe email when
    # given; consult name only when it is not" — because a genuinely NEW record whose email search
    # comes up empty still falls through to the name search, and two unrelated records that merely
    # share a display name can silently merge just because the caller also passed a fresh email.
    #
    # `dedup_probe`, when set, is a PURE function of the raw NIL args that narrows `dedup_keys` down
    # to the ordered subset that actually applies to THIS call — e.g. `("email",)` when an email was
    # given, `("name",)` when it was not, never both. `edge.py` only ever CALLS this (via
    # `dedup_probe_keys`); it never branches on a business field name itself, so the mechanism stays
    # vendor-neutral. Left `None` (the default) preserves every existing verb's behaviour exactly:
    # `dedup_probe_keys` falls back to trying the full `dedup_keys` tuple, first hit wins.
    dedup_probe: (
        Callable[[dict[str, Any]], tuple[str | tuple[str, ...] | tuple[tuple[str, str, Any], ...], ...]]
        | None
    ) = None
    method: str | None = (
        None  # for op="method": the Odoo model method to invoke (e.g. "message_post")
    )
    # for op="method": the inverse method that reverses it (action_post→button_draft). When set, the
    # effect is COMPENSABLE — ROLLBACK previews+runs the inverse — instead of IRREVERSIBLE.
    reverse_method: str | None = None
    # NIL arg keys this verb can actually write/use. When declared, PROPOSE flags any provided arg
    # outside this set as `ignored` — so an unwritable field (e.g. country) is never silently accepted.
    supported_args: tuple[str, ...] = ()
    # many2one (dropdown) args this verb resolves to a backend reference id before writing. Each entry
    # is (nil_arg, native_field, model): the edge looks the human value up in `model` and writes the
    # resolved id into `native_field` — never the raw string (a relational field rejects text). The
    # supported_args list still gates acceptance; references only declares HOW the value is resolved.
    references: tuple[tuple[str, str, str], ...] = ()
    # DECLARED NUMERIC PRECONDITIONS: args that must parse to a number STRICTLY GREATER THAN ZERO for
    # the effect to be computable at all. A landed cost over a received quantity of zero is not a cost
    # of zero — it is not a cost. Without this the verb translated to an EMPTY doc, the edge wrote it,
    # and the shim reported `executed` / `claim: success` for a cost it had never set. Declared here,
    # refused at PROPOSE: "I wrote it" and "I could not compute it" must never be the same answer.
    positive: tuple[str, ...] = ()
    # for op="create": the native field this verb stamps the COMMIT's attempt key into, so the act
    # becomes DETERMINISTICALLY ADDRESSABLE in the backing system. Without one, a create whose
    # answer is lost cannot be told apart from one that never happened, and the only honest answer
    # to a transport failure is a refusal (RECOVERY-MATRIX "case C"). WITH one, the edge asks the
    # backing system first and returns the record that already exists instead of minting a second.
    #
    # Only a field that is REALLY a free-text reference on the model may be named here, and the edge
    # additionally checks the LIVE schema before stamping — an Odoo whose model lacks the field
    # degrades to the honest refusal, never to a rejected write.
    #     purchase.order  → `origin`  (Source Document; already a supported arg on this verb)
    #     account.payment → `ref`     (Reference; this adapter already writes it, translate.py:631)
    #     account.move    → `ref`     (Reference; the invoice-create verbs set nothing else there)
    # Verbs with NO honest home for a key, left unstamped on purpose rather than inventing a field:
    #     crm.create_lead        (crm.lead has no reference/source Char — nothing to query on)
    #     commerce.create_product(product.product's `default_code` IS the SKU, a business key that
    #                             belongs to the product, not to one commit attempt)
    #     resource.create        (arbitrary target — no field can be declared for a model we do not
    #                             know until the request arrives)
    idempotency_field: str | None = None
    #: C3.7 — the recovery classification for a verb that carries NO attempt key. Not every keyless
    #: write is unsafe: the owner's rule admits two shapes, and only one of them needs a key.
    #:
    #:   "convergent"  a stable pre-existing identity + SET semantics. `sale.confirm_order` on an
    #:                 existing order_id is the same state however many times it runs, and the edge
    #:                 already reports `already_confirmed` rather than confirming twice. Safe
    #:                 BECAUSE replay is a no-op, not because anyone asks.
    #:   "none"        genuinely nothing to query on. A permanent human-gate, and a CORRECT final
    #:                 answer — not technical debt.
    #:
    #: Left unset ⇒ the plane reads UNKNOWN, which it treats exactly as UNSAFE. That is the
    #: fail-closed floor, and the whole point of declaring: an unasked question and a considered
    #: "there is nothing to query" must never read the same.
    recovery_shape: str | None = None
    #: Why — carried onto the wire so a human reading the verdict sees the reasoning, not a label.
    recovery_note: str = ""

    def missing(self, args: dict[str, Any]) -> list[str]:
        return [field for field in self.required if not args.get(field)]

    def dedup_probe_keys(
        self, args: dict[str, Any]
    ) -> tuple[str | tuple[str, ...] | tuple[tuple[str, str, Any], ...], ...]:
        """The dedup_keys entries to actually probe FOR THIS CALL. Delegates to `dedup_probe` when
        the verb declares one (a per-call narrowing — see its docstring above); otherwise returns the
        full declared `dedup_keys` unchanged, which is every verb's behaviour today."""
        return self.dedup_probe(args) if self.dedup_probe is not None else self.dedup_keys

    def nonpositive(self, args: dict[str, Any]) -> list[str]:
        """Declared `positive` args that are absent, non-numeric, or <= 0 — the uncomputable set."""
        out: list[str] = []
        for f in self.positive:
            try:
                if float(args[f]) <= 0:
                    out.append(f)
            except (KeyError, TypeError, ValueError):
                out.append(f)
        return out


@dataclass(frozen=True)
class QueryVerb:
    """A READ. It has no tier and no reversibility — not because they were forgotten, but because a
    read has no effect to govern or to undo. `effect="read"` is declared EXPLICITLY (in describe and
    in the manifest's `read_verbs` map) precisely so a consumer never has to infer it from absence:
    absence, in the effect plane, fail-closes to IRREVERSIBLE, and a read must never register as an
    unreversible write."""

    verb: str
    run: Callable[[SystemClient, dict[str, Any]], dict[str, Any]]
    # Args the read cannot run without — the edge refuses a call that lacks one instead of crashing.
    required: tuple[str, ...] = ()
    target: str = ""  # the native model this read is about (declaration, not dispatch)
    returns: str = "rows"  # "rows" | "document" — what the caller gets back
    report: str = ""  # for returns="document": the ERP report that renders it (never rendered here)
    effect: str = "read"

    def missing(self, args: dict[str, Any]) -> list[str]:
        return [field for field in self.required if not args.get(field)]


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
    doc: dict[str, Any] = {
        "name": args["name"],
        "type": args.get("lead_type", "opportunity"),
    }
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
    for nil_key, odoo_key in (
        ("email", "email"),
        ("phone", "phone"),
        ("company", "company_name"),
    ):
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
            (0, 0, _invoice_line(ln)) for ln in lines if isinstance(ln, dict)
        ]
    return doc


def _invoice_line(ln: dict[str, Any]) -> dict[str, Any]:
    """One invoice line. `product_id` (when given) lets Odoo derive the income account/taxes so the
    invoice is POSTABLE; name/quantity/price_unit are the manual fallback."""
    line: dict[str, Any] = {
        "name": ln.get("name", ""),
        "quantity": _maybe_float(ln.get("quantity", 1)),
        "price_unit": _maybe_float(ln.get("price_unit", 0)),
    }
    if ln.get("product_id"):
        line["product_id"] = _maybe_int(ln["product_id"])
    if ln.get(
        "account_id"
    ):  # explicit income/expense account (when no product drives it)
        line["account_id"] = _maybe_int(ln["account_id"])
    return line


def _to_native_method_only(_args: dict[str, Any]) -> dict[str, Any]:
    """A workflow-method verb (validate / confirm) writes no fields — the record id is the first
    required arg, consumed by the edge; the method itself drives the state transition in Odoo."""
    return {}


def _to_native_register_payment(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → an Odoo `account.payment` (a customer/supplier payment record). Defaults to an
    inbound customer payment; partner_type is derived from payment_type so the record is valid."""
    ptype = args.get("payment_type", "inbound")
    doc: dict[str, Any] = {
        "payment_type": ptype,
        "partner_type": "customer" if ptype == "inbound" else "supplier",
        "amount": _maybe_float(args.get("amount", 0)),
    }
    if args.get("partner_id"):
        doc["partner_id"] = _maybe_int(args["partner_id"])
    if args.get("journal_id"):
        doc["journal_id"] = _maybe_int(args["journal_id"])
    if args.get("date"):
        doc["date"] = args["date"]
    if args.get("ref"):
        doc["ref"] = args["ref"]
    return doc


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
    rows = client.search(
        "res.country", domain, fields=("id", "name", "code"), limit=300
    )
    return {"target": "res.country", "count": len(rows), "items": rows}


def _run_get_contact_by_phone(
    client: SystemClient, args: dict[str, Any]
) -> dict[str, Any]:
    """Hot-path identity lookup: the WhatsApp entry point IS a phone number. Indexed exact match on
    `phone`, falling back to Odoo's normalized `phone_sanitized` (handles formatting differences) —
    never a full-scan via list_contacts. An unmatched number is a valid empty read, not an error."""
    phone = str(args.get("phone", "")).strip()
    if not phone:
        return {"target": "res.partner", "count": 0, "items": []}
    rows = _safe_search(client, [["phone", "=", phone]])
    if (
        not rows
    ):  # the stored value may be formatted differently than the inbound number
        rows = _safe_search(client, [["phone_sanitized", "=", phone]])
    return {"target": "res.partner", "count": len(rows), "items": rows}


def _safe_search(client: SystemClient, domain: list[list[Any]]) -> list[dict[str, Any]]:
    """A read-side search that degrades to empty if the backend rejects the domain (e.g. a field the
    model lacks) — a lookup must never turn a missing match into a 500."""
    try:
        return client.search("res.partner", domain, limit=10)
    except SystemError:
        return []


# Contact card fields a comms flow needs: the human name plus every channel we can reach (email +
# phone/mobile). `phone_sanitized` is Odoo's normalized form, useful for correlating an inbound WA
# number. Odoo has no native "whatsapp" field, so `phone`/`mobile` ARE the WhatsApp reach.
_CONTACT_FIELDS = ("id", "name", "email", "phone", "mobile", "phone_sanitized")
# The universally-valid subset: Odoo 18 REMOVED res.partner.mobile, and phone_sanitized needs the
# phone_validation module — asking search_read for a missing field faults the WHOLE call.
_CONTACT_FIELDS_BASE = ("id", "name", "email", "phone")


def _search_contact_cards(
    client: SystemClient, domain: list[list[Any]], *, limit: int = 10
) -> list[dict[str, Any]]:
    """res.partner search projected to the contact card, DEGRADING to the universal base set when
    this Odoo build lacks the optional channel fields. Without the fallback, every projected
    lookup (find_contact / get_contact) silently returned empty against Odoo 18 tenants — the
    per-field SystemError catch upstream read as 'no match' (proven live: ValueError: Invalid
    field 'mobile' on 'res.partner')."""
    try:
        return client.search("res.partner", domain, fields=_CONTACT_FIELDS, limit=limit)
    except SystemError:
        return client.search(
            "res.partner", domain, fields=_CONTACT_FIELDS_BASE, limit=limit
        )


def _run_find_contact(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    """Resolve a client by free text (name / email / phone) → their contact card WITH email + phone.
    Powers 'send email to client X' in chat, and correlates an inbound reply back to a known contact.

    Matches the query against each human-facing field separately and unions the hits (deduped by id,
    first-match order) — a backend-agnostic OR that needs no polish-notation domain support. A blank
    query lists recent contacts. Any field the model lacks degrades to skipped — a lookup never 500s."""
    q = str(args.get("query", "")).strip()
    if not q:
        try:
            rows = _search_contact_cards(client, [])
        except SystemError:
            rows = []
        return {"target": "res.partner", "count": len(rows), "items": rows}
    seen: dict[Any, dict[str, Any]] = {}
    for field in ("name", "email", "phone"):
        try:
            hits = _search_contact_cards(client, [[field, "ilike", q]])
        except SystemError:
            continue
        for row in hits:
            seen.setdefault(row.get("id"), row)
    items = list(seen.values())[:10]
    return {"target": "res.partner", "count": len(items), "items": items}


def _run_get_contact(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    """Fetch ONE contact by id → its full channel card (email + phone). Resolves the awaited client's
    channel before a send and hydrates a decision card. A missing/non-numeric id or absent record is a
    valid empty read, never an error."""
    cid = str(args.get("contact_id", "")).strip()
    try:
        pid = int(cid)
    except ValueError:
        return {"target": "res.partner", "found": False, "item": None}
    try:
        rows = _search_contact_cards(client, [["id", "=", pid]], limit=1)
    except SystemError:
        rows = []
    return {
        "target": "res.partner",
        "found": bool(rows),
        "item": rows[0] if rows else None,
    }


# ── write verb constants (referenced by packs.py; aggregated into WRITE_VERBS at module bottom) ──
CRM_CREATE_LEAD = WriteVerb(
    verb="crm.create_lead",
    recovery_shape="none",
    recovery_note="a lead carries no free-text reference field and no dedup key — there is nothing to query on",
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
)
CRM_CREATE_CONTACT = WriteVerb(
    verb="crm.create_contact",
    recovery_shape="convergent",
    recovery_note="upsert on a REQUIRED dedup key (C3.5 refuses a keyless call)",
    tier="MEDIUM",
    doctype="res.partner",
    op="upsert",
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
)
CRM_UPDATE_CONTACT = WriteVerb(
    verb="crm.update_contact",
    recovery_shape="convergent",
    recovery_note="updates an existing contact_id — the same fields written twice are the same state",
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
    supported_args=(
        "contact_id",
        "name",
        "phone",
        "email",
        "comment",
        "company",
        "country",
    ),
    references=(("country", "country_id", "res.country"),),
)
CRM_LOG_NOTE = WriteVerb(
    verb="crm.log_note",
    recovery_shape="none",
    recovery_note="appends a mail.message; the body is searchable but nothing correlates it to one attempt",
    tier="MEDIUM",
    doctype="res.partner",
    op="method",
    method="message_post",
    required=("contact_id", "body"),
    to_native=_to_native_log_note,
    preview=lambda a: {
        "en": f'Log a note on contact {a.get("contact_id", "")}: "{a.get("body", "")}"',
        "ar": f"تسجيل ملاحظة على جهة الاتصال {a.get('contact_id', '')}: «{a.get('body', '')}»",
    },
    entity_type="contact",
)
CRM_UPDATE_LEAD_STAGE = WriteVerb(
    verb="crm.update_lead_stage",
    recovery_shape="convergent",
    recovery_note="sets a stage on an existing lead_id; SET semantics",
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
)
CRM_DELETE_LEAD = WriteVerb(
    verb="crm.delete_lead",
    recovery_shape="convergent",
    recovery_note="deletes an existing id, verified by read-back",
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
)
CRM_DELETE_CONTACT = WriteVerb(
    verb="crm.delete_contact",
    recovery_shape="convergent",
    recovery_note="deletes an existing id, and the edge re-reads to confirm ABSENCE — a second delete is a no-op",
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
)
# ── semantic verbs across module groups (Phase 6) ─────────────────────────────────────────────────
ACCOUNT_CREATE_INVOICE = WriteVerb(
    verb="account.create_invoice",
    tier="HIGH",
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
    idempotency_field="ref",
)
STOCK_VALIDATE_PICKING = WriteVerb(
    verb="stock.validate_picking",
    recovery_shape="convergent",
    recovery_note="validates an existing picking_id; the moves are committed once",
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
)
SALE_CONFIRM_ORDER = WriteVerb(
    verb="sale.confirm_order",
    recovery_shape="convergent",
    recovery_note="confirms an existing order_id, and already reports `already_confirmed` rather than confirming twice",
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
)
ACCOUNT_POST_INVOICE = WriteVerb(
    verb="account.post_invoice",
    recovery_shape="convergent",
    recovery_note="posts an existing invoice_id; a posted move re-posts to the same state",
    tier="HIGH",
    doctype="account.move",
    op="method",
    method="action_post",
    reverse_method="button_draft",
    required=("invoice_id",),
    to_native=_to_native_method_only,
    preview=lambda a: {
        "en": f"Post invoice {a.get('invoice_id', '')} (commit it to the books)",
        "ar": f"ترحيل الفاتورة {a.get('invoice_id', '')} (تثبيتها في الدفاتر)",
    },
    entity_type="invoice",
)
ACCOUNT_REGISTER_PAYMENT = WriteVerb(
    verb="account.register_payment",
    tier="HIGH",
    doctype="account.payment",
    op="create",
    required=("partner_id", "amount"),
    to_native=_to_native_register_payment,
    preview=lambda a: {
        "en": f"Register a {a.get('payment_type', 'inbound')} payment of {a.get('amount', '')}"
        + f" for partner {a.get('partner_id', '')}",
        "ar": f"تسجيل دفعة ({a.get('payment_type', 'inbound')}) بمبلغ {a.get('amount', '')}"
        + f" للطرف {a.get('partner_id', '')}",
    },
    entity_type="payment",
    supported_args=(
        "partner_id",
        "amount",
        "payment_type",
        "journal_id",
        "date",
        "ref",
    ),
    idempotency_field="ref",
)


# ── the CANONICAL Wosool verb vocabulary (SSOT plan Phase E) ──────────────────────────────────────
# The baseline capability catalog is backend-agnostic: its cycles call ONE canonical verb set
# (crm.create_client, services.create_invoice, commerce.record_payment, commerce.create_product,
# procurement.create_purchase_invoice) so the identical bundle serves workspace #1 and #10,000.
# Each adapter DECLARES those canonical verbs and maps them internally to its native op + arg
# transform — the catalog never learns backend names. These are first-class WriteVerbs (declared in
# describe, routed, tiered, previewed), not string aliases: an alias alone cannot fix arg shapes.
def _to_native_services_create_invoice(args: dict[str, Any]) -> dict[str, Any]:
    """Canonical {client_id, currency, description, amount?, lines?} → Odoo customer invoice
    (`account.move`, out_invoice). `description` becomes a single invoice line when no explicit
    lines are given; `currency` is resolved to `currency_id` by the reference resolver."""
    doc: dict[str, Any] = {"move_type": "out_invoice"}
    if args.get("client_id"):
        doc["partner_id"] = _maybe_int(args["client_id"])
    if args.get("invoice_date"):
        doc["invoice_date"] = args["invoice_date"]
    lines = args.get("lines") or []
    if lines:
        doc["invoice_line_ids"] = [
            (0, 0, _invoice_line(ln)) for ln in lines if isinstance(ln, dict)
        ]
    elif args.get("description") or args.get("amount") is not None:
        # Plan B5: an amount with no description must STILL produce the priced line — a line-less
        # invoice is rejected on post, and dropping the money silently was the live failure.
        doc["invoice_line_ids"] = [
            (
                0,
                0,
                {
                    "name": str(args.get("description") or "Service"),
                    "quantity": 1,
                    "price_unit": _maybe_float(args.get("amount", 0)),
                },
            )
        ]
    return doc


def _to_native_commerce_record_payment(args: dict[str, Any]) -> dict[str, Any]:
    """Canonical {invoice_id, amount, partner_id?, date?} → Odoo `account.payment` (inbound
    customer payment). The invoice reference travels in `ref` so the human trail ties the payment
    to its invoice; reconciliation stays an Odoo-side act."""
    doc: dict[str, Any] = {
        "payment_type": "inbound",
        "partner_type": "customer",
        "amount": _maybe_float(args.get("amount", 0)),
    }
    if args.get("invoice_id"):
        doc["ref"] = f"invoice {args['invoice_id']}"
    if args.get("partner_id"):
        doc["partner_id"] = _maybe_int(args["partner_id"])
    if args.get("journal_id"):
        doc["journal_id"] = _maybe_int(args["journal_id"])
    if args.get("date"):
        doc["date"] = args["date"]
    return doc


def _to_native_commerce_create_product(args: dict[str, Any]) -> dict[str, Any]:
    """Canonical {name, price, sku} → Odoo `product.product`."""
    doc: dict[str, Any] = {"name": args.get("name", "")}
    if args.get("price") is not None:
        doc["list_price"] = _maybe_float(args["price"])
    if args.get("sku"):
        doc["default_code"] = args["sku"]
    return doc


def _to_native_set_landed_cost(args: dict[str, Any]) -> dict[str, Any]:
    """M9: landed cost per unit → Odoo `product.product.standard_price`.

    The arithmetic is on data we already hold (the PO subtotal + freight + insurance + duties, over
    the quantity received). The adapter computes it, never the model.

    An uncomputable landed cost is REFUSED, not degraded. This function used to return `{}` when the
    quantity was absent or zero — an EMPTY write. The edge dutifully wrote nothing, read nothing back
    (`fields_written` was falsy, so the read-after-write branch was skipped) and reported
    `claim: success, verified: true`. The platform announced it had set a cost it had not set, and
    Finance would have found out from the ledger, a month later. The premise is declared on the verb
    (`positive=("po_amount", "qty")`) and enforced at PROPOSE; this raise is the backstop for a caller
    that reaches the translation directly, and surfaces as an honest terminal failure."""
    qty = _maybe_float(args.get("qty") or 0)
    if not qty or qty <= 0:
        raise SystemError(
            "landed cost is undefined without a positive received quantity (qty > 0)"
        )
    po_amount = _maybe_float(args.get("po_amount") or 0)
    freight = _maybe_float(args.get("freight") or 0)
    insurance = _maybe_float(args.get("insurance") or 0)
    duties = _maybe_float(args.get("duties") or 0)
    per_unit = (po_amount + freight + insurance + duties) / qty
    return {"standard_price": round(per_unit, 4)}


def _to_native_procurement_create_purchase_invoice(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Canonical {supplier_id, currency, invoice_date?, lines?} → Odoo VENDOR BILL
    (`account.move`, in_invoice) — the purchase invoice in Odoo's vocabulary."""
    doc: dict[str, Any] = {"move_type": "in_invoice"}
    if args.get("supplier_id"):
        doc["partner_id"] = _maybe_int(args["supplier_id"])
    if args.get("invoice_date"):
        doc["invoice_date"] = args["invoice_date"]
    lines = args.get("lines") or []
    if lines:
        doc["invoice_line_ids"] = [
            (0, 0, _invoice_line(ln)) for ln in lines if isinstance(ln, dict)
        ]
    return doc


WOSOOL_CREATE_CLIENT = WriteVerb(
    verb="crm.create_client",
    recovery_shape="convergent",
    recovery_note="upsert on a REQUIRED dedup key (C3.5 refuses a keyless call), so the key is the stable identity",
    tier="MEDIUM",
    doctype="res.partner",
    op="upsert",
    required=("name",),
    to_native=_to_native_create_contact,
    preview=lambda a: {
        "en": f"Create or update client “{a.get('name', '')}”"
        + (f" <{a['email']}>" if a.get("email") else ""),
        "ar": f"إنشاء أو تحديث العميل «{a.get('name', '')}»"
        + (f" <{a['email']}>" if a.get("email") else ""),
    },
    entity_type="contact",
    dedup_keys=("email", "phone"),
    supported_args=("name", "phone", "email", "company", "is_company"),
)
WOSOOL_CREATE_INVOICE = WriteVerb(
    verb="services.create_invoice",
    tier="HIGH",
    doctype="account.move",
    op="create",
    required=("client_id",),
    to_native=_to_native_services_create_invoice,
    preview=lambda a: {
        "en": f"Create a customer invoice for client {a.get('client_id', '')}"
        + (f" — “{a['description']}”" if a.get("description") else ""),
        "ar": f"إنشاء فاتورة للعميل {a.get('client_id', '')}"
        + (f" — «{a['description']}»" if a.get("description") else ""),
    },
    entity_type="invoice",
    supported_args=(
        "client_id",
        "currency",
        "description",
        "amount",
        "invoice_date",
        "lines",
    ),
    references=(("currency", "currency_id", "res.currency"),),
    idempotency_field="ref",
)
WOSOOL_RECORD_PAYMENT = WriteVerb(
    verb="commerce.record_payment",
    tier="HIGH",
    doctype="account.payment",
    op="create",
    required=("invoice_id", "amount"),
    to_native=_to_native_commerce_record_payment,
    preview=lambda a: {
        "en": f"Record a payment of {a.get('amount', '')} against invoice {a.get('invoice_id', '')}",
        "ar": f"تسجيل دفعة بمبلغ {a.get('amount', '')} على الفاتورة {a.get('invoice_id', '')}",
    },
    entity_type="payment",
    supported_args=("invoice_id", "amount", "partner_id", "journal_id", "date"),
    idempotency_field="ref",
)
WOSOOL_CREATE_PRODUCT = WriteVerb(
    verb="commerce.create_product",
    recovery_shape="none",
    recovery_note="`default_code` is the SKU — the PRODUCT's key, not one attempt's; reusing it as a witness would conflate two different identities",
    tier="MEDIUM",
    doctype="product.product",
    op="create",
    required=("name",),
    to_native=_to_native_commerce_create_product,
    preview=lambda a: {
        "en": f"Create product “{a.get('name', '')}”"
        + (f" at {a['price']}" if a.get("price") is not None else ""),
        "ar": f"إنشاء منتج «{a.get('name', '')}»"
        + (f" بسعر {a['price']}" if a.get("price") is not None else ""),
    },
    entity_type="product",
    supported_args=("name", "price", "sku"),
)
WOSOOL_SET_LANDED_COST = WriteVerb(
    # M9: landed cost per unit — arithmetic on data we already hold, written back to Odoo's SSOT.
    # The adapter does the arithmetic (never the model); the per-unit cost = (PO subtotal + freight +
    # insurance + duties) / quantity received lands on the product's `standard_price` — the cost of
    # goods Finance reads. op="update": we mutate an existing product, not mint one.
    verb="commerce.set_landed_cost",
    recovery_shape="convergent",
    recovery_note="writes a cost onto an existing product_id; SET semantics",
    tier="HIGH",
    doctype="product.product",
    op="update",
    # The PO amount and the received quantity are REQUIRED, and both must be positive: this verb is
    # arithmetic, and there is no landed cost without them. (It required only `product_id` before, so
    # a cycle could call it with nothing to compute and be told it had succeeded.) The Daftara binding
    # of this same capability declares the identical contract — two bindings, one contract.
    required=("product_id", "po_amount", "qty"),
    positive=("po_amount", "qty"),
    to_native=_to_native_set_landed_cost,
    preview=lambda a: {
        "en": f"Set landed cost on product {a.get('product_id', '')}",
        "ar": f"تحديد تكلفة الوصول للمنتج {a.get('product_id', '')}",
    },
    entity_type="product",
    supported_args=(
        "product_id",
        "po_amount",
        "freight",
        "insurance",
        "duties",
        "qty",
        "sku",
    ),
)
WOSOOL_CREATE_PURCHASE_INVOICE = WriteVerb(
    verb="procurement.create_purchase_invoice",
    tier="HIGH",
    doctype="account.move",
    op="create",
    required=("supplier_id",),
    to_native=_to_native_procurement_create_purchase_invoice,
    preview=lambda a: {
        "en": f"Record a purchase invoice (vendor bill) from supplier {a.get('supplier_id', '')}",
        "ar": f"تسجيل فاتورة مشتريات من المورد {a.get('supplier_id', '')}",
    },
    entity_type="invoice",
    supported_args=("supplier_id", "currency", "invoice_date", "lines"),
    references=(("currency", "currency_id", "res.currency"),),
    idempotency_field="ref",
)

# ── crm.* query verb constants ────────────────────────────────────────────────────────────────────
CRM_LIST_LEADS = QueryVerb(verb="crm.list_leads", run=_run_list_leads)
CRM_LIST_CONTACTS = QueryVerb(verb="crm.list_contacts", run=_run_list_contacts)
CRM_LIST_STAGES = QueryVerb(verb="crm.list_stages", run=_run_list_stages)
CRM_LIST_COUNTRIES = QueryVerb(verb="crm.list_countries", run=_run_list_countries)
CRM_GET_CONTACT_BY_PHONE = QueryVerb(
    verb="crm.get_contact_by_phone", run=_run_get_contact_by_phone
)
CRM_FIND_CONTACT = QueryVerb(verb="crm.find_contact", run=_run_find_contact)
CRM_GET_CONTACT = QueryVerb(verb="crm.get_contact", run=_run_get_contact)


# ── purchasing verb constants ────────────────────────────────────────────────────────────────────
def _to_native_create_purchase_order(args: dict[str, Any]) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    if args.get("partner_id"):
        doc["partner_id"] = _maybe_int(args["partner_id"])
    if args.get("date_order"):
        doc["date_order"] = args["date_order"]
    if args.get("origin"):
        doc["origin"] = args["origin"]
    lines = args.get("lines") or []
    if lines:
        doc["order_line"] = [
            (
                0,
                0,
                {
                    "product_id": _maybe_int(ln.get("product_id")),
                    "product_qty": _maybe_float(ln.get("product_qty", 1)),
                    "product_uom": _maybe_int(ln.get("product_uom", 1)),
                    "price_unit": _maybe_float(ln.get("price_unit", 0)),
                    **(
                        {"taxes_id": [(6, 0, ln["taxes_id"])]}
                        if ln.get("taxes_id")
                        else {}
                    ),
                    **(
                        {"date_planned": ln["date_planned"]}
                        if ln.get("date_planned")
                        else {}
                    ),
                },
            )
            for ln in lines
            if isinstance(ln, dict)
        ]
    return doc


PURCHASE_CREATE_ORDER = WriteVerb(
    verb="purchase.create_order",
    tier="MEDIUM",
    doctype="purchase.order",
    op="create",
    required=("partner_id",),
    to_native=_to_native_create_purchase_order,
    preview=lambda a: {
        "en": f"Create purchase order for partner {a.get('partner_id', '')}",
        "ar": f"إنشاء أمر شراء للمورد {a.get('partner_id', '')}",
    },
    entity_type="purchase_order",
    supported_args=("partner_id", "date_order", "origin", "lines"),
    idempotency_field="origin",
)
PURCHASE_DELETE_ORDER = WriteVerb(
    # The INVERSE of purchase.create_order — and the reason the dual PO can be a saga at all.
    #
    # `purchase.create_order` was declared IRREVERSIBLE (absent from COMPENSATIONS), which meant the
    # M8 definition of done — "refusing the Daftara leg compensates per envelope" — was not merely
    # unproven, it was IMPOSSIBLE: there was nothing to compensate the Odoo leg WITH. A purchase order
    # that this verb created is a DRAFT (confirming it is a separate, HIGH-tier verb), and a draft PO
    # unlinks cleanly in Odoo. If the world has moved on and the order was confirmed or received,
    # Odoo REFUSES the unlink — and the edge reports that refusal as a terminal failure rather than
    # swallowing it, which is exactly the behaviour a compensation must have.
    verb="purchase.delete_order",
    recovery_shape="convergent",
    recovery_note="deletes an existing id, verified by read-back",
    tier="HIGH",  # deleting is HIGH in this adapter, as every other delete verb is
    doctype="purchase.order",
    op="delete",
    required=("order_id",),
    to_native=lambda _a: {},
    preview=lambda a: {
        "en": f"Delete purchase order {a.get('order_id', '')}",
        "ar": f"حذف أمر الشراء {a.get('order_id', '')}",
    },
    entity_type="purchase_order",
    supported_args=("order_id",),
)
# ── the ERP's OWN rendered documents (READ) ──────────────────────────────────────────────────────
# Creating a PO or a bill in Odoo returns the RECORD. The DOCUMENT — the PDF the vendor receives — is
# rendered by a SEPARATE call, from Odoo's own QWeb report, and only Odoo's render carries Odoo's
# numbering, tax lines, payment terms, logo and legal footer. A document we render ourselves from the
# record is a DIFFERENT document from the one in the system of record: a fabrication, and it breaks
# the rule that what a human approves on the card is byte-identical to what the vendor receives.
#
# These verbs fetch the ERP's bytes and hash them. They compose nothing. They are READS: no tier, no
# reversibility, absent from COMPENSATIONS and from the manifest's write-verb map by DESIGN.
_PURCHASE_ORDER_REPORT = "purchase.report_purchaseorder"
_VENDOR_BILL_REPORT = "account.report_invoice"
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def _document(
    client: SystemClient, *, target: str, report_ref: str, record_id: Any, fallback: str
) -> dict[str, Any]:
    """Fetch the ERP's rendered document for one record → {document: {...}} or a structured refusal.

    Every failure path REFUSES with DOCUMENT_UNAVAILABLE. There is no fallback renderer here and there
    must never be one: "I could not get the official document" and "here is the official document"
    must not produce the same answer."""
    rid = str(record_id or "").strip()
    if not rid:
        return {"outcome": "refused", "code": "DOCUMENT_UNAVAILABLE", "message": "no record id given"}
    try:
        rows = client.search(target, [["id", "=", _maybe_int(rid)]], fields=("id", "name"), limit=1)
    except SystemError as exc:
        return {"outcome": "refused", "code": "DOCUMENT_UNAVAILABLE", "message": str(exc)}
    if not rows:
        return {
            "outcome": "refused",
            "code": "DOCUMENT_UNAVAILABLE",
            "message": f"no {target} with id {rid}",
        }
    try:
        pdf = client.render_report(report_ref, target, rid)
    except SystemError as exc:
        return {"outcome": "refused", "code": "DOCUMENT_UNAVAILABLE", "message": str(exc)}
    if not pdf:
        return {
            "outcome": "refused",
            "code": "DOCUMENT_UNAVAILABLE",
            "message": f"{report_ref} rendered no bytes for {target}/{rid}",
        }
    # The filename is the RECORD'S OWN reference (Odoo's PO number / bill number) — never minted here.
    ref = str(rows[0].get("name") or f"{fallback}-{rid}")
    return {
        "document": {
            "filename": _SAFE_FILENAME.sub("_", ref)[:120] + ".pdf",
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(pdf).decode("ascii"),
            "sha256": hashlib.sha256(pdf).hexdigest(),  # of the ERP's bytes — what the ledger attests
            "size": len(pdf),
            "source": {
                "system": "odoo_crm",
                "report": report_ref,
                "target": target,
                "id": rid,
            },
        }
    }


def _run_get_order_document(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    return _document(
        client,
        target="purchase.order",
        report_ref=_PURCHASE_ORDER_REPORT,
        record_id=args.get("order_id"),
        fallback="PO",
    )


def _run_get_invoice_document(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    return _document(
        client,
        target="account.move",
        report_ref=_VENDOR_BILL_REPORT,
        record_id=args.get("invoice_id"),
        fallback="INV",
    )


PURCHASE_GET_ORDER_DOCUMENT = QueryVerb(
    verb="purchase.get_order_document",
    run=_run_get_order_document,
    required=("order_id",),
    target="purchase.order",
    returns="document",
    report=_PURCHASE_ORDER_REPORT,
)
ACCOUNT_GET_INVOICE_DOCUMENT = QueryVerb(
    verb="account.get_invoice_document",
    run=_run_get_invoice_document,
    required=("invoice_id",),
    target="account.move",
    returns="document",
    report=_VENDOR_BILL_REPORT,
)

# ── history.movements / history.changes (entity-history E1 follow-up) — Odoo's OWN append-only
# facts as governed READS. Neither was ever read by this adapter before: `stock.move` (the stock
# ledger) is referenced nowhere else in this file, and `mail.message` (chatter) was write-only
# (`crm.log_note` posts via `message_post`; nothing listed it). A failed read here REFUSES
# `UPSTREAM_UNAVAILABLE` — never `items: []` ("nothing here" and "I could not look" must never be
# the same answer; see `_run_get_contact`'s swallow, the bad precedent NOT copied here).
_HISTORY_LIMIT_DEFAULT = 50
_HISTORY_LIMIT_MAX = 200
_HISTORY_ORDER = "date desc, id desc"

_MOVEMENTS_MODEL = "stock.move"
# Each entry is a GROUP of alternate native spellings for the same logical field, preference order
# first — `stock.move` renamed both across Odoo versions. Live-gated on a real Odoo 19 (saas~19.4)
# via `fields_get`: `quantity` carries the moved qty (17+; `quantity_done` is the OLDER spelling,
# absent on 19) and the unit is `uom_id` (NOT `product_uom`, which does not exist on 19 either — an
# earlier guess). Both spellings are requested; `_history_fields` keeps whichever this instance
# actually has, and a group is reported `missing` ONLY when NEITHER spelling exists.
_MOVEMENTS_FIELD_GROUPS: tuple[tuple[str, ...], ...] = (
    ("id",),
    ("reference",),
    ("quantity", "quantity_done"),
    ("uom_id", "product_uom"),
    ("location_id",),
    ("location_dest_id",),
    ("picking_id",),
    ("origin",),
    ("date",),
    ("write_date",),
)
_CHANGES_MODEL = "mail.message"
_CHANGES_FIELD_GROUPS: tuple[tuple[str, ...], ...] = (
    ("id",), ("message_type",), ("subtype_id",), ("author_id",),
    ("subject",), ("body",), ("date",), ("write_date",),
)
# Live-gate scouting finding (W5 T1 follow-up): a Product's chatter is normally posted on its
# TEMPLATE (`product.template`), not on the variant (`product.product`) that `RESOURCES["Product"]`
# resolves to — so a naive `model=product.product, res_id=<variant id>` domain comes back EMPTY on
# a real instance while `describe()` says `changes: true`. That is exactly the failure this adapter
# must never produce: "nothing happened" read as "I looked in the wrong place". For Product ONLY,
# `history.changes` reads BOTH models and merges.
_PRODUCT_TEMPLATE_MODEL = "product.template"


def _require_history_int(raw: Any) -> int | None:
    """Strict int coercion for a value that MUST already be numeric. Unlike `_maybe_int` (which
    passes odd input through unchanged, to keep a WriteVerb's `to_native` crash-free), this is the
    validation gate: non-numeric input here is the caller's refusal to report, not a value to carry
    through unchanged."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _odoo_dt_to_iso(value: Any) -> str | None:
    """Odoo's naive-UTC `"YYYY-MM-DD HH:MM:SS"` -> `"YYYY-MM-DDTHH:MM:SSZ"`. Falsy passes through
    as None — an absent timestamp is never fabricated as a string."""
    if not value:
        return None
    s = str(value).strip()
    return s if "T" in s else s.replace(" ", "T", 1) + "Z"


def _iso_to_odoo_dt(value: str) -> str | None:
    """ISO-8601 (`...Z` / `...+00:00` / naive) -> Odoo's naive-UTC `"YYYY-MM-DD HH:MM:SS"` for use
    in a domain triple. None when the input does not parse to that shape at all."""
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1]
    s = s.replace("T", " ")
    if "." in s:
        s = s.split(".", 1)[0]
    plus = s.find("+", 10)  # skip the date's own leading '-' separators
    if plus != -1:
        s = s[:plus]
    minus_tz = s.rfind("-")
    if minus_tz > 10:  # a trailing "-HH:MM" offset (the date's own '-'s are at index 4 and 7)
        s = s[:minus_tz].rstrip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", s):
        return s
    return None


def _flatten_m2o(value: Any) -> Any:
    """Odoo hands back a many2one over XML-RPC as `[id, "Label"]`; flatten to the label string. A
    falsy value (False/None — no linked record) is genuinely absent, never fabricated as ""."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[1]
    return value if value else None


def _m2o_id(value: Any) -> int | None:
    """The ID half of a many2one value (`[id, "Label"]` over XML-RPC; a bare int is also accepted,
    for a stand-in that stores it unwrapped). Falsy → no linked record, never fabricated as 0."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return _require_history_int(value[0])
    return _require_history_int(value) if value else None


def _first_available(row: dict[str, Any], *keys: str) -> Any:
    """The value of the first key among `keys` carrying a truthy value — for a logical field Odoo
    has RENAMED across versions (e.g. the unit on `stock.move`: `uom_id` on 17+, `product_uom` on
    older — only one spelling is ever actually populated on a given instance). None when none of
    them carry a value; never fabricated."""
    for k in keys:
        v = row.get(k)
        if v:
            return v
    return None


def _plain_text(html: Any, *, limit: int = 280) -> str | None:
    """`mail.message.body` is HTML; the answer carries plain text, never markup, bounded to
    `limit` chars — a chatter note can be arbitrarily long; the history item is a summary of it,
    not the record itself."""
    if not html:
        return None
    text = re.sub(r"<[^>]+>", " ", str(html))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _history_fields(
    client: SystemClient, model: str, groups: tuple[tuple[str, ...], ...]
) -> tuple[tuple[str, ...], list[str]]:
    """(fields, missing) for a CURATED set of logical fields, each declared as a GROUP of one or
    more alternate native spellings in preference order (the same idea `read_plane.py`'s projection
    uses, generalized: intersect against `client.schema(model)` when it answers a real shape, keep
    every alternate verbatim when it answers empty/None — a fake, or an instance exposing no
    metadata; `id` is always kept). `fields` carries every alternate THIS instance's live schema
    actually has (a group with version drift, e.g. `stock.move`'s `quantity`/`quantity_done` or
    `uom_id`/`product_uom`, may contribute more than one name — the item builder picks the real one
    at read time). `missing` names a group ONLY when NONE of its alternates exist: a version that
    answers under a DIFFERENT but KNOWN spelling must never be reported as missing a field it
    actually has under another name — that would turn a healthy instance into a false alarm."""
    field_meta = client.schema(model)
    if not field_meta:
        return tuple(name for group in groups for name in group), []
    available = {f.get("name") for f in field_meta}
    fields: list[str] = []
    missing: list[str] = []
    for group in groups:
        if "id" in group:
            fields.append("id")
            continue
        present = [name for name in group if name in available]
        if present:
            fields.extend(present)
        else:
            missing.append(group[0])  # the curated/primary spelling names the gap, not every alias
    if not fields:  # nothing curated matched this schema at all — degrade to the full verbatim ask
        return tuple(name for group in groups for name in group), missing
    return tuple(fields), missing


def _history_limit(args: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    """(limit, refusal) — exactly one is set."""
    raw = args.get("limit")
    if raw is None or raw == "":
        return _HISTORY_LIMIT_DEFAULT, None
    n = _require_history_int(raw)
    if n is None or not (1 <= n <= _HISTORY_LIMIT_MAX):
        return None, {
            "outcome": "refused", "code": "BAD_ARGS",
            "message": f"limit must be an integer in 1..{_HISTORY_LIMIT_MAX}, got {raw!r}",
        }
    return n, None


def _history_before(args: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """(before, refusal) in Odoo's own naive-UTC format — exactly one of (before, refusal) is set;
    `before` itself may legitimately be None (the arg was not given)."""
    raw = args.get("before")
    if raw is None or raw == "":
        return None, None
    native = _iso_to_odoo_dt(str(raw))
    if native is None:
        return None, {
            "outcome": "refused", "code": "BAD_ARGS",
            "message": f"before must be ISO-8601 UTC, got {raw!r}",
        }
    return native, None


def _movement_item(row: dict[str, Any]) -> dict[str, Any]:
    quantity = row.get("quantity")
    if quantity is None:
        quantity = row.get("quantity_done")
    return {
        "id": row.get("id"),
        "at": _odoo_dt_to_iso(row.get("date")),
        "reference": row.get("reference") or None,
        "quantity": quantity,
        # `uom_id` on Odoo 17+ (live-gated on a real 19); `product_uom` on older instances only.
        "uom": _flatten_m2o(_first_available(row, "uom_id", "product_uom")),
        "from": _flatten_m2o(row.get("location_id")),
        "to": _flatten_m2o(row.get("location_dest_id")),
        "picking": _flatten_m2o(row.get("picking_id")),
        "origin": row.get("origin") or None,
        "write_date": _odoo_dt_to_iso(row.get("write_date")),
    }


def _change_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "at": _odoo_dt_to_iso(row.get("date")),
        "message_type": row.get("message_type") or None,
        "subtype": _flatten_m2o(row.get("subtype_id")),
        "author": _flatten_m2o(row.get("author_id")),
        "subject": row.get("subject") or None,
        "body": _plain_text(row.get("body")),
        "write_date": _odoo_dt_to_iso(row.get("write_date")),
    }


def _run_history_movements(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    resource = str(args.get("resource") or "")
    if resource != "Product":
        return {
            "outcome": "refused", "code": "UNKNOWN_RESOURCE",
            "message": f"history.movements only tracks Product, got {resource!r}",
        }
    rid = _require_history_int(args.get("record_id"))
    if rid is None:
        return {
            "outcome": "refused", "code": "BAD_ARGS",
            "message": f"record_id must be numeric, got {args.get('record_id')!r}",
        }
    limit, limit_refusal = _history_limit(args)
    if limit_refusal is not None:
        return limit_refusal
    before_native, before_refusal = _history_before(args)
    if before_refusal is not None:
        return before_refusal
    from odoo_nil_adapter import governance  # lazy: translate<->governance would cycle at import

    if not governance.module_enabled(_MOVEMENTS_MODEL):
        return {
            "outcome": "refused", "code": "MODULE_DISABLED",
            "message": f"{_MOVEMENTS_MODEL} is not in the operator-enabled module scope",
        }
    domain: list[list[Any]] = [["product_id", "=", rid], ["state", "=", "done"]]
    if before_native is not None:
        domain.append(["date", "<", before_native])
    fields, missing_fields = _history_fields(client, _MOVEMENTS_MODEL, _MOVEMENTS_FIELD_GROUPS)
    try:
        rows = client.search(_MOVEMENTS_MODEL, domain, fields=fields, limit=limit, order=_HISTORY_ORDER)
    except SystemError as exc:
        return {"outcome": "refused", "code": "UPSTREAM_UNAVAILABLE", "message": str(exc)}
    items = [_movement_item(r) for r in rows]
    return {
        "resource": resource, "record_id": str(rid), "model": _MOVEMENTS_MODEL,
        "items": items, "count": len(items), "limit": limit, "missing_fields": missing_fields,
    }


# ── history.movements_feed — the same `stock.move` ledger, asked the OTHER question: not "what
# happened to this product" (newest first) but "what has Odoo moved since I last looked", across all
# products, oldest first, from a resumable cursor. It EXPOSES Odoo's data; deciding what a move means
# (issue, receipt, adjustment, transfer) and recording it is the engine's job.
#
# The cursor is (write_date, id), strict on both:
# - not id alone: a move is created when its transfer is confirmed and becomes `done` later, so a
#   LOW id can complete after a higher one, and an id cursor would skip it;
# - not write_date alone: one validated transfer writes all its moves with the SAME write_date, so a
#   paged write_date cursor would stall or skip inside it.
# A move written again after `done` comes back again; the consumer dedupes on the move id.
_FEED_ORDER = "write_date asc, id asc"
_FEED_FIELD_GROUPS: tuple[tuple[str, ...], ...] = _MOVEMENTS_FIELD_GROUPS + (
    ("product_id",),
    ("location_usage",),       # related location_id.usage (internal/supplier/customer/inventory/…)
    ("location_dest_usage",),
    ("picking_code",),         # related picking_type_id.code (incoming/outgoing/internal)
    ("is_inventory",),         # True on an inventory-adjustment move
    ("write_uid",),            # the LAST writer; on a done move usually whoever validated it
)


def _feed_item(row: dict[str, Any]) -> dict[str, Any]:
    """`_movement_item` plus what a consumer of ALL products needs to interpret a move on its own:
    the product and location ids (labels are not keys), both location usages, the transfer type, the
    inventory-adjustment flag and the last writer. A field this instance lacks is None — never a
    fabricated False or ""."""
    item = _movement_item(row)
    writer = row.get("write_uid")
    item.update({
        "product_id": _m2o_id(row.get("product_id")),
        "product": _flatten_m2o(row.get("product_id")),
        "from_id": _m2o_id(row.get("location_id")),
        "to_id": _m2o_id(row.get("location_dest_id")),
        "from_usage": row.get("location_usage") or None,
        "to_usage": row.get("location_dest_usage") or None,
        "picking_type": row.get("picking_code") or None,
        "is_inventory": bool(row["is_inventory"]) if "is_inventory" in row else None,
        "last_written_by": (
            {"id": _m2o_id(writer), "name": _flatten_m2o(writer)} if writer else None
        ),
    })
    return item


def _feed_cursor(args: dict[str, Any]) -> tuple[tuple[str, int] | None, dict[str, Any] | None]:
    """((odoo write_date, id), refusal) — exactly one is set."""
    native = _iso_to_odoo_dt(str(args.get("after_write_date") or ""))
    if native is None:
        return None, {
            "outcome": "refused", "code": "BAD_ARGS",
            "message": f"after_write_date must be ISO-8601 UTC, got {args.get('after_write_date')!r}",
        }
    raw_id = args.get("after_id")
    after_id = 0 if raw_id is None or raw_id == "" else _require_history_int(raw_id)
    if after_id is None or after_id < 0:
        return None, {
            "outcome": "refused", "code": "BAD_ARGS",
            "message": f"after_id must be a non-negative integer, got {raw_id!r}",
        }
    return (native, after_id), None


def _run_history_movements_feed(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    cursor, cursor_refusal = _feed_cursor(args)
    if cursor_refusal is not None:
        return cursor_refusal
    limit, limit_refusal = _history_limit(args)
    if limit_refusal is not None:
        return limit_refusal
    from odoo_nil_adapter import governance  # lazy: translate<->governance would cycle at import

    if not governance.module_enabled(_MOVEMENTS_MODEL):
        return {
            "outcome": "refused", "code": "MODULE_DISABLED",
            "message": f"{_MOVEMENTS_MODEL} is not in the operator-enabled module scope",
        }
    after_date, after_id = cursor  # type: ignore[misc]
    domain: list[Any] = [
        ["state", "=", "done"],
        "|", ["write_date", ">", after_date],
        "&", ["write_date", "=", after_date], ["id", ">", after_id],
    ]
    fields, missing_fields = _history_fields(client, _MOVEMENTS_MODEL, _FEED_FIELD_GROUPS)
    try:
        rows = client.search(_MOVEMENTS_MODEL, domain, fields=fields, limit=limit, order=_FEED_ORDER)
    except SystemError as exc:
        return {"outcome": "refused", "code": "UPSTREAM_UNAVAILABLE", "message": str(exc)}
    items = [_feed_item(r) for r in rows]
    if rows:
        last = rows[-1]
        next_cursor = {"after_write_date": _odoo_dt_to_iso(last.get("write_date")),
                       "after_id": _require_history_int(last.get("id"))}
    else:
        next_cursor = {"after_write_date": _odoo_dt_to_iso(after_date), "after_id": after_id}
    return {
        "model": _MOVEMENTS_MODEL, "items": items, "count": len(items), "limit": limit,
        "next": next_cursor, "complete": len(items) < limit, "missing_fields": missing_fields,
    }


def _product_changes_domains(
    client: SystemClient, variant_model: str, rid: int
) -> tuple[list[tuple[str, list[list[Any]]]], dict[str, Any] | None]:
    """(domains, refusal) for a Product's `history.changes` — Odoo chatter for a product is
    normally posted on the TEMPLATE (`product.template`), not the variant `RESOURCES["Product"]`
    resolves to. Reading only the variant's `mail.message` rows comes back empty on a real
    instance while `describe()` says `changes: true` — "nothing happened" read as "I looked in the
    wrong place". So: resolve the variant's `product_tmpl_id` with ONE read, and search BOTH
    models. The resolving read ITSELF failing (a SystemError) refuses `UPSTREAM_UNAVAILABLE` —
    never a guess at completeness; the variant simply not existing (no rows, no exception) is a
    legitimate "nothing to resolve", so the caller proceeds on the variant domain alone."""
    domains: list[tuple[str, list[list[Any]]]] = [
        (variant_model, [["model", "=", variant_model], ["res_id", "=", rid]])
    ]
    try:
        rows = client.search(variant_model, [["id", "=", rid]], fields=("id", "product_tmpl_id"), limit=1)
    except SystemError as exc:
        return [], {"outcome": "refused", "code": "UPSTREAM_UNAVAILABLE", "message": str(exc)}
    tmpl_id = _m2o_id(rows[0].get("product_tmpl_id")) if rows else None
    if tmpl_id is not None:
        domains.append(
            (_PRODUCT_TEMPLATE_MODEL, [["model", "=", _PRODUCT_TEMPLATE_MODEL], ["res_id", "=", tmpl_id]])
        )
    return domains, None


def _run_history_changes(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    resource = str(args.get("resource") or "")
    if resource not in RESOURCES:
        return {
            "outcome": "refused", "code": "UNKNOWN_RESOURCE",
            "message": f"unknown resource: {resource!r}",
        }
    rid = _require_history_int(args.get("record_id"))
    if rid is None:
        return {
            "outcome": "refused", "code": "BAD_ARGS",
            "message": f"record_id must be numeric, got {args.get('record_id')!r}",
        }
    limit, limit_refusal = _history_limit(args)
    if limit_refusal is not None:
        return limit_refusal
    before_native, before_refusal = _history_before(args)
    if before_refusal is not None:
        return before_refusal
    model = native_model(resource)
    from odoo_nil_adapter import governance  # lazy: translate<->governance would cycle at import

    if not governance.module_enabled(model):
        return {
            "outcome": "refused", "code": "MODULE_DISABLED",
            "message": f"{model} is not in the operator-enabled module scope",
        }
    if resource == "Product":
        domains, resolve_refusal = _product_changes_domains(client, model, rid)
        if resolve_refusal is not None:
            return resolve_refusal
    else:
        domains = [(model, [["model", "=", model], ["res_id", "=", rid]])]
    if before_native is not None:
        domains = [(m, d + [["date", "<", before_native]]) for m, d in domains]
    fields, missing_fields = _history_fields(client, _CHANGES_MODEL, _CHANGES_FIELD_GROUPS)
    seen_ids: set[Any] = set()
    rows: list[dict[str, Any]] = []
    for _m, domain in domains:
        try:
            found = client.search(_CHANGES_MODEL, domain, fields=fields, limit=limit, order=_HISTORY_ORDER)
        except SystemError as exc:
            return {"outcome": "refused", "code": "UPSTREAM_UNAVAILABLE", "message": str(exc)}
        for row in found:
            row_id = row.get("id")
            if row_id in seen_ids:  # merging variant + template chatter must never double-count
                continue
            seen_ids.add(row_id)
            rows.append(row)
    rows.sort(key=lambda r: (str(r.get("date") or ""), r.get("id") or 0), reverse=True)
    rows = rows[:limit]
    items = [_change_item(r) for r in rows]
    return {
        "resource": resource, "record_id": str(rid), "model": _CHANGES_MODEL,
        "items": items, "count": len(items), "limit": limit, "missing_fields": missing_fields,
    }


HISTORY_MOVEMENTS = QueryVerb(
    verb="history.movements",
    run=_run_history_movements,
    required=("resource", "record_id"),
    target=_MOVEMENTS_MODEL,
    returns="rows",
)
HISTORY_CHANGES = QueryVerb(
    verb="history.changes",
    run=_run_history_changes,
    required=("resource", "record_id"),
    target=_CHANGES_MODEL,
    returns="rows",
)
# Kept OUT of `_NIL_QUERY_VERBS` (the generic `nil.*` intent surface) — these two are curated,
# always-on verbs beside it, exactly like the document reads above. `packs.py` is untouched: a
# neighbour branch (`feat/primary-supplier`) edits it, and these two are model-fixed (stock.move /
# native_model(resource)), not gated by any write pack.
_HISTORY_QUERY_VERBS: dict[str, QueryVerb] = {
    "history.movements": HISTORY_MOVEMENTS,
    "history.changes": HISTORY_CHANGES,
    "history.movements_feed": QueryVerb(
        verb="history.movements_feed",
        run=_run_history_movements_feed,
        required=("after_write_date",),
        target=_MOVEMENTS_MODEL,
        returns="rows",
    ),
}

PURCHASE_CONFIRM_ORDER = WriteVerb(
    verb="purchase.confirm_order",
    recovery_shape="convergent",
    recovery_note="confirms an existing order_id; the state is the same however many times it runs",
    tier="HIGH",
    doctype="purchase.order",
    op="method",
    method="button_confirm",
    reverse_method="button_cancel",
    required=("order_id",),
    to_native=_to_native_method_only,
    preview=lambda a: {
        "en": f"Confirm purchase order {a.get('order_id', '')}",
        "ar": f"تأكيد أمر الشراء {a.get('order_id', '')}",
    },
    entity_type="purchase_order",
)

# ── procurement.link_supplier / unlink_supplier (Task 1.3, D37/D38/O3 — fix round 1) ──────────────
# Coordinator ruling (fix round 1): a write not on the governed wire does not exist for this
# platform. The earlier `translate.run()` direct-execution surface is GONE — these are now real
# `WriteVerb` entries in WRITE_VERBS, declared in `/nil/v0.1/describe`, committed through `edge.py`'s
# ordinary PROPOSE→COMMIT exactly like every other curated verb.
#
# `to_native` is PURE (no I/O, per translate.py:1): the lookup this task originally needed
# (`sku` → `product_tmpl_id`) moves to the CALLER. Args are `{product_ref, supplier_ref, price?,
# min_qty?, delay_days?, sku?}` — `product_ref` is the product TEMPLATE's id in the record system,
# `supplier_ref` the partner id (both strings the caller already resolved: the control plane / the
# os-server hold both in the Odoo mirror, `sku_by_tmpl`/`sku_by_variant`). `sku` is a display-only
# passthrough this verb never writes (it earns its slot in `supported_args` so it is never flagged
# `ignored` at PROPOSE — carrying it is deliberate, not a mistake).
def _to_native_link_supplier(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → the `product.supplierinfo` fields this verb writes. `sku`, if given, is display-only
    and never lands here — see the module comment above."""
    doc: dict[str, Any] = {}
    if args.get("product_ref"):
        doc["product_tmpl_id"] = _maybe_int(args["product_ref"])
    if args.get("supplier_ref"):
        doc["partner_id"] = _maybe_int(args["supplier_ref"])
    if args.get("price") is not None:
        doc["price"] = _maybe_float(args["price"])
    if args.get("min_qty") is not None:
        doc["min_qty"] = _maybe_float(args["min_qty"])
    if args.get("delay_days") is not None:
        doc["delay"] = _maybe_float(args["delay_days"])
    return doc


PROCUREMENT_LINK_SUPPLIER = WriteVerb(
    verb="procurement.link_supplier",
    recovery_shape="convergent",
    recovery_note=(
        "upserts on the REQUIRED compound pair (product_tmpl_id, partner_id) — edge.py's op=upsert "
        "dedup now probes tuple entries as an AND-group (fix round 1), so a retry converges on the "
        "existing link instead of minting a second one"
    ),
    tier="MEDIUM",
    doctype="product.supplierinfo",
    op="upsert",
    required=("product_ref", "supplier_ref"),
    to_native=_to_native_link_supplier,
    preview=lambda a: {
        "en": f"Link supplier {a.get('supplier_ref', '')} to product "
        f"{a.get('sku') or a.get('product_ref', '')}"
        + (f" at {a['price']}" if a.get("price") is not None else ""),
        "ar": f"ربط المورد {a.get('supplier_ref', '')} بالمنتج "
        f"{a.get('sku') or a.get('product_ref', '')}"
        + (f" بسعر {a['price']}" if a.get("price") is not None else ""),
    },
    entity_type="product_supplier_link",
    # A compound (AND-probed) key — see the generalized `dedup_keys` handling in edge.py's op=upsert
    # dispatch. Every OTHER dedup_keys entry in this file stays a single field name (OR semantics,
    # unchanged); this is the one compound entry.
    dedup_keys=(("product_tmpl_id", "partner_id"),),
    supported_args=("product_ref", "supplier_ref", "price", "min_qty", "delay_days", "sku"),
)

# `procurement.unlink_supplier` identifies the record by ITS OWN id (`link_ref`), obtained by the
# caller from a prior `nil.search`/`nil.get` on the `ProductSupplier` resource (which already
# exposes the raw `id`) — the SAME idiom every other curated delete verb in this file uses
# (`crm.delete_lead`/`lead_id`, `crm.delete_contact`/`contact_id`, `purchase.delete_order`/
# `order_id`). This is a deliberate choice, not an oversight: `edge.py`'s `op="delete"` dispatch
# resolves its record via `_resolve_id(client, doctype, args[required[0]])`, which is a SINGLE-FIELD
# lookup — using `product_ref` (a product TEMPLATE's id) or `supplier_ref` (a partner id) directly
# there would risk an id COLLISION across unrelated models (Odoo ids are per-table sequences, so
# `product.template` #7 and `product.supplierinfo` #7 can both exist as unrelated rows) — an
# `op="delete"` call could target and remove the WRONG supplier-link. `link_ref` is the
# `product.supplierinfo` record's own id: an exact match, safe by construction.
PROCUREMENT_UNLINK_SUPPLIER = WriteVerb(
    verb="procurement.unlink_supplier",
    # IRREVERSIBLE, and this is a DEVIATION from the fix round 1 ruling ("unlink COMPENSABLE by
    # re-create from the before-image") — see the concern filed in the Task 1.3 report for the full
    # argument. Short version: `edge.py`'s curated `op="delete"` branch never reads the record before
    # deleting it (unlike its `op="update"` branch, which captures a before-image via
    # `_before_image_reversal`, and unlike the GENERIC `resource.delete` path, which already does
    # `before = client.get(...)` for exactly this reason) — so NOTHING beyond the deleted record's own
    # id survives into `compensate()`'s `result` for any curated delete, this one included. Declaring
    # COMPENSABLE while `compensate()` cannot actually rebuild `product_ref`/`supplier_ref`/`price`/
    # `min_qty`/`delay_days` from an id alone would be exactly the kind of governance-envelope lie
    # this codebase's own history (M8's dual-PO saga, the C3 RESOURCES-shape fix) exists to prevent —
    # a declared reversibility the platform cannot actually perform. The honest fix is one more line
    # in edge.py's delete branch (capture `before = client.get(verb.doctype, record_id)` before the
    # delete, mirroring the update branch exactly) — outside this round's ONE permitted edit.
    recovery_shape="convergent",
    recovery_note="deletes an existing link_ref, and the edge re-reads to confirm ABSENCE — a second unlink is a no-op",
    # C1 (final review): every OTHER delete in this adapter is HIGH (crm.delete_lead, crm.delete_contact,
    # purchase.delete_order — this adapter's own stated rule is `_CRUD_TIERS = {..., "delete": "HIGH"}`,
    # governance.py:60). This verb was the single MEDIUM exception, and MEDIUM auto-executes with no
    # human in the loop — exactly wrong for an IRREVERSIBLE delete that captures no before-image. The
    # fix-round-1 ruling that accepted IRREVERSIBLE here did so ON THE PREMISE that HIGH tier parks it
    # for a human (progress.md:74); declaring MEDIUM broke that premise. HIGH, like every other delete.
    tier="HIGH",
    doctype="product.supplierinfo",
    op="delete",
    required=("link_ref",),
    to_native=_to_native_delete,
    preview=lambda a: {
        "en": f"Unlink supplier from product-supplier link {a.get('link_ref', '')}",
        "ar": f"إلغاء ربط المورد من سجل ربط المورد بالمنتج {a.get('link_ref', '')}",
    },
    entity_type="product_supplier_link",
    supported_args=("link_ref",),
)

# ── procurement.create_supplier (Task 1.3b): a res.partner Odoo can be the SUPPLIER record for ────
# `ManageSuppliers.create` (baseline capability) routes here when the operator's Supplier authority
# is Odoo. Odoo has no separate "vendor" table — a supplier IS a `res.partner` with `supplier_rank`
# raised above zero, the exact counter `Supplier`'s base domain already reads (`RESOURCE_DOMAINS`
# above). So this verb writes the SAME model `crm.create_contact` writes, deliberately: it stamps
# `supplier_rank: 1` and leaves `customer_rank` untouched (Odoo's own default, 0) — a record this
# verb creates is a vendor, never a customer, and a `Supplier` read finds it immediately because the
# base domain and this verb's write agree on the same field.
def _to_native_create_supplier(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → an Odoo `res.partner` scoped as a supplier. `is_company` is always True (a supplier
    is a business, never a person) — unlike `crm.create_contact`, it is not an optional input here."""
    doc: dict[str, Any] = {"name": args["name"], "is_company": True, "supplier_rank": 1}
    for nil_key, odoo_key in (
        ("email", "email"),
        ("phone", "phone"),
        ("vat", "vat"),
    ):
        if args.get(nil_key):
            doc[odoo_key] = args[nil_key]
    return doc


# Fix round 1 (Task 1.3b, D-concern-1): a plain OR-probed `dedup_keys=("email","name")` would try
# `email` first and, on a miss, ALSO try `name` — so a genuinely NEW supplier whose email search
# comes up empty could still merge into an UNRELATED existing supplier that merely shares a display
# name. That is wrong: when the caller gave an email, `name` must never be consulted at all. The
# ruling is exact — email given → probe email ONLY; email absent → probe name ONLY, never both in
# the same call. `dedup_probe` (translate.py's `WriteVerb`, `edge.py`'s `dedup_probe_keys`) expresses
# that as a pure per-call narrowing so `edge.py` stays vendor-neutral: it only ever calls this
# function, it never itself knows that "email" or "name" are the fields in play.
#
# I4 (final review): the probe used to search `res.partner` UNSCOPED by rank, so a genuinely new
# supplier's email could converge onto an existing CUSTOMER-ONLY partner and silently rename it while
# the signed preview still read "Create supplier «X»". Ruling: scope EVERY probed entry by the SAME
# base domain the read side already enforces for `Supplier` (`RESOURCE_DOMAINS["Supplier"]`,
# `supplier_rank > 0`) — a customer-only partner (no `supplier_rank`, or `0`) is never a match, so the
# create proceeds and mints its own record; Odoo's own uniqueness (if any) answers from there.
#
# I4 fix round 2 (re-review Important finding): the first cut scoped this via a COMPOUND
# equality-group — `(<identity field>, "supplier_rank")`, probed as `identity = <value> AND
# supplier_rank = 1` — reusing `procurement.link_supplier`'s existing compound-key mechanism as-is.
# That is WRONG: `supplier_rank` in real Odoo is a CUMULATIVE counter (Odoo increments it on
# confirmed purchase orders/vendor bills), not a boolean pinned at 1 — a genuinely pre-existing
# supplier that has been used on more than one PO can carry `supplier_rank` of 2 or higher, and
# `supplier_rank = 1` would not match it, silently minting a DUPLICATE `res.partner` on a repeat call
# with that supplier's own email. Odoo's own `> 0` domain (the exact predicate `RESOURCE_DOMAINS`
# already declares for `Supplier` reads) is a RANGE, which the compound equality-group cannot express
# at all — no value `to_native` could ever write makes `field = value` mean `field > 0` for every
# value greater than zero.
#
# The fix: return a compound group of fully-resolved DOMAIN TRIPLES instead —
# `(identity, "=", <the given value>), ("supplier_rank", ">", 0)` — which `edge.py`'s
# `_is_domain_group` recognizes and ANDs verbatim as `[[identity, "=", value], ["supplier_rank", ">",
# 0]]`, matching ANY existing supplier regardless of its actual rank, while still excluding a
# customer-only partner (rank 0, or the field absent entirely — `_triple`'s `>` comparison treats a
# missing/`None` value as failing the predicate). `edge.py` still only inspects TUPLE SHAPE and calls
# this function — it never itself knows "supplier_rank" is the field in play.
#
# A person who wants to promote an existing CUSTOMER to a supplier does so explicitly (e.g. a future
# verb, or a direct `resource.update`) — this verb's dedup is deliberately narrow, not a general
# partner-merge tool.
def _dedup_probe_create_supplier(args: dict[str, Any]) -> tuple[tuple[tuple[str, str, Any], ...], ...]:
    identity = "email" if args.get("email") else "name"
    return (((identity, "=", args.get(identity)), ("supplier_rank", ">", 0)),)


PROCUREMENT_CREATE_SUPPLIER = WriteVerb(
    verb="procurement.create_supplier",
    # Convergent by the SAME C3.5 discipline as `crm.create_contact`/`crm.create_client`: an upsert
    # with nothing to deduplicate on is a blind create wearing an upsert's name. The difference here
    # is that `name` (this verb's only REQUIRED arg) is itself the fallback member of `dedup_keys` —
    # so, unlike create_contact (whose dedup set is email/phone, neither required), this verb can
    # never actually reach C3.5's keyless-create refusal: a name is always present. That is a
    # deliberate reading of the brief ("dedup on email when given, else on name"), not an oversight —
    # a real supplier always has a name, and probing it when no email was given converges a retry
    # onto the same vendor instead of minting a duplicate purely because the caller wrote the name
    # only once.
    recovery_shape="convergent",
    recovery_note=(
        "upserts on email when given, name ONLY when it is not (dedup_probe narrows the call to "
        "exactly one of the two — never both — so an unrelated supplier sharing a display name can "
        "never merge just because a fresh email was also given; fix round 1, D-concern-1), and EACH "
        "probe is a domain-triple group scoped by supplier_rank>0 — a true range, not equality — so "
        "a customer-only partner is never the match AND an existing supplier of any rank still "
        "converges (I4, fix round 2)"
    ),
    tier="MEDIUM",
    doctype="res.partner",
    op="upsert",
    required=("name",),
    to_native=_to_native_create_supplier,
    preview=lambda a: {
        "en": f"Create supplier “{a.get('name', '')}”"
        + (f" <{a['email']}>" if a.get("email") else ""),
        "ar": f"إنشاء مورد «{a.get('name', '')}»"
        + (f" <{a['email']}>" if a.get("email") else ""),
    },
    entity_type="supplier",
    # Documentation / wire (`describe`, manifest, the "identity" field) only — `dedup_probe` is
    # ALWAYS set below and (`name` being required) NEVER returns empty for this verb, so the C3.5
    # fallback that would otherwise consult this tuple is UNREACHABLE at runtime; the actual per-call
    # domain (equality on email-or-name AND supplier_rank > 0 — a real range, I4 fix round 2) can only
    # be expressed with a concrete per-call value, which a static declaration cannot carry. Kept as
    # symbolic field-name pairs so the wire still names the two fields this verb keys on.
    dedup_keys=(("email", "supplier_rank"), ("name", "supplier_rank")),
    dedup_probe=_dedup_probe_create_supplier,
)

# ── the universal read data plane (nil.*): lean, filtered, paginated, governed ────────────────────
# These delegate to the shared `ReadPlane` (projection + byte-cap-refuse + capability fallback + read
# authz + export/bulk gating). The edge dispatches them through QUERY_VERBS like any read verb; engine
# refusals come back as structured `{outcome: refused, code, message}` answers, never 500s.
from datetime import UTC, datetime  # noqa: E402

from nilscript.dataplane import (  # noqa: E402
    Binding,
    BulkApprovalRequired,
    CapabilityUnsupported,
    Intent,
    IntentResolver,
    InvalidFilter,
    ResultTooLarge,
)

from odoo_nil_adapter.read_plane import build_read_plane  # noqa: E402

import weakref  # noqa: E402

_READ_REFUSALS = (
    ResultTooLarge,
    InvalidFilter,
    CapabilityUnsupported,
    BulkApprovalRequired,
)
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
    return {
        "outcome": "refused",
        "code": getattr(exc, "code", "ERROR"),
        "message": getattr(exc, "message", str(exc)),
    }


# Default to an EMPTY grant (`()`), so the ReadPlane redacts every field classified sensitive
# (salary / VAT / IBAN / credit on financial & HR models) unless the caller explicitly `reveal`s it.
# `None` would mean "unrestricted" and leak those by default — discovery must never do that.
def _grant(args: dict[str, Any]) -> tuple[str, ...]:
    reveal = args.get("reveal")
    return tuple(reveal) if reveal else ()


# D37 (Task 1.1): every nil.* read verb used to hand `args["target"]` straight to the plane — a
# business name (`Customer`) the backend has no table for → CAPABILITY_UNSUPPORTED, even though
# `nil.intent` (below) already resolved the identical name correctly. `_resolved_target` makes the
# SAME call `nil.intent` makes (`_OdooBindings().resolve_target`) the FIRST thing every read verb does,
# so a business name resolves before it ever reaches the plane — never after.
def _resolved_target(args: dict[str, Any]) -> str:
    return _OdooBindings().resolve_target(args.get("target", ""))


# I1 (final review): the `Supplier` projection (packs.py's own `vat`-carrying entry, keyed by the
# BUSINESS name) was unreachable — every real read resolved to the native model (`res.partner`)
# BEFORE calling the plane, so `describe_target` always matched the shared `res.partner` entry
# (Customer's) and a `Supplier` read could never see `vat`, even with `reveal`. Ruling: the plane's
# projection is chosen by the RESOURCE when the caller named one (an EXACT declared key — "Supplier",
# not a case-insensitive or aliased guess), falling back to the fully resolved native model for a
# native name or an unrecognized string — today's behaviour, unchanged. `OdooReadBackend.describe_target`
# (read_plane.py) already tries the exact name first, then the native model; this is the missing half
# that actually HANDS it the exact name instead of resolving it away first.
def _target_for_plane(args: dict[str, Any]) -> str:
    about = args.get("target", "")
    return about if about in RESOURCES else _resolved_target(args)


# Odoo triple op → the NIL predicate op it round-trips through (the read plane's own `_to_domain`
# undoes this on the way back out). Only the ops a `base_filter_for` entry can plausibly use.
_TRIPLE_OP_TO_NIL: dict[str, str] = {
    "=": "eq", "!=": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "in": "in",
}


def _filter_with_base(args: dict[str, Any]) -> list[dict[str, Any]]:
    """D37 (Task 1.2): prepend the resource's fixed base domain (e.g. `supplier_rank > 0`) to the
    caller's filter, keyed on the ORIGINAL business name — never the resolved model, which by then
    can no longer tell `Customer` and `Supplier` apart. A native model name (no RESOURCES entry)
    contributes no base predicate, so today's behaviour is unchanged."""
    base = [
        {"field": field, "op": _TRIPLE_OP_TO_NIL[op], "value": value}
        for field, op, value in base_filter_for(args.get("target", ""))
    ]
    return [*base, *(args.get("filter") or [])]


def _run_nil_search(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    resource = args.get("target", "")
    try:
        result = _plane(client).search(
            _target_for_plane(args),
            filter=_translate_product_supplier_filter(client, resource, _filter_with_base(args)),
            fields=args.get("fields"),
            limit=int(args.get("limit") or 50),
            cursor=args.get("cursor"),
            grant_fields=_grant(args),
        )
    except _READ_REFUSALS as exc:
        return _refusal(exc)
    return _project_product_supplier_items(client, resource, result)


def _run_nil_count(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return _plane(client).count(_target_for_plane(args), filter=_filter_with_base(args))
    except _READ_REFUSALS as exc:
        return _refusal(exc)


def _id_satisfies_domain(plane: Any, resource: str, native: str, record_id: Any) -> bool:
    """Whether `record_id` on `native` satisfies `resource`'s fixed base domain (fix round 1, C2/C1) —
    checked via a SCOPED EXISTENCE COUNT rather than inspecting a fetched record's fields, because the
    domain's own field (`supplier_rank`) is not part of the resource's curated read projection and a
    projected `get()` result would not carry it at all. No domain declared (or no id given) is
    vacuously true — a native model name keeps today's unfiltered behaviour.

    I3 (final review): this used to fail OPEN — a refused scoped count returned `True`, letting the
    caller's own (unscoped) `get` proceed and potentially hand back a record outside the domain. The
    comment that reasoning relied on only holds for a TARGET-level refusal (unprovisioned/out-of-scope
    model), where the caller's own read fails identically either way; it does NOT hold for a refusal
    caused by this count's own extra predicate. Fail CLOSED: propagate the refusal — every caller
    (`_run_nil_get`'s own try/except, `_run_nil_intent` via `_ScopedPlane`) already turns a propagated
    `_READ_REFUSALS` member into a structured `refused` outcome, never a 500 and never a silent
    `{"found": False}` that would read exactly like an ordinary miss."""
    base = base_filter_for(resource)
    if not base or record_id is None:
        return True
    base_preds = [
        {"field": field, "op": _TRIPLE_OP_TO_NIL[op], "value": value} for field, op, value in base
    ]
    result = plane.count(native, filter=[*base_preds, {"field": "id", "op": "eq", "value": record_id}])
    return bool(result.get("count", 0))


def _run_nil_get(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    try:
        resource = args.get("target", "")
        native = _resolved_target(args)
        record_id = args.get("id")
        # Fix round 1, C2: `ReadPlane.get()` takes no `filter` — resolving the target alone (Task 1.1)
        # let a `Supplier` fetch return ANY partner, including a pure customer. A record outside the
        # resource's base domain answers the SAME shape a missing id answers — never the record itself.
        if not _id_satisfies_domain(_plane(client), resource, native, record_id):
            return {"found": False, "id": record_id}
        rec = _plane(client).get(
            _target_for_plane(args),
            record_id=record_id,
            fields=args.get("fields"),
            grant_fields=_grant(args),
        )
        if rec is None:
            return {"found": False, "id": record_id}
        return _project_product_supplier_row(client, resource, rec)
    except _READ_REFUSALS as exc:
        return _refusal(exc)


def _run_nil_aggregate(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return _plane(client).aggregate(
            _target_for_plane(args),
            filter=_filter_with_base(args),
            group_by=args["group_by"],
            metrics=tuple(args.get("metrics") or ("count",)),
        )
    except _READ_REFUSALS as exc:
        return _refusal(exc)


def _run_nil_export(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    try:
        handle = _plane(client).export(
            _target_for_plane(args),
            filter=_filter_with_base(args),
            fields=args.get("fields"),
            tenant=str(args.get("tenant") or "default"),
            now=datetime.now(UTC),
            approved=bool(args.get("approved")),
        )
        return {
            "handle": handle.handle,
            "format": handle.format,
            "rows": handle.rows,
            "bytes": handle.bytes,
            "schema": handle.schema,
            "expires_at": handle.expires_at.isoformat(),
        }
    except _READ_REFUSALS as exc:
        return _refusal(exc)


class _OdooBindings:
    """Ontology → Odoo vocabulary. The agent asks about a `Product`; Odoo stores `product.product`.

    Wave A's adapter-local half: the business names the thing, the backend keeps its own word. The
    map is `RESOURCES` — the SAME table this adapter declares in `describe` — applied here so a read
    by business name (`about="Product"`) resolves to the native model instead of being handed to Odoo
    verbatim (which has no table `Product` → CAPABILITY_UNSUPPORTED). An `about` we do not recognize
    (`crm.stage`, a raw native model) passes THROUGH unchanged, so the engine can serve it or refuse
    it for itself — never a guess. `resolve_attr` stays identity: Odoo field names are already native
    (`name`, `phone`), and the read plane derives the projection.

    This was the production gap behind "اعرض لي المنتجات" refusing while `about="res.partner"` worked:
    the adapter DECLARED the mapping but built its resolver with `IdentityResolver`, so it never
    applied it. Mirrors daftara's `_DaftraBindings`."""

    def resolve_target(self, about: str) -> str:
        if not about:
            return about
        if about in RESOURCES:  # the canonical business resource name (Product, Customer, …)
            return RESOURCES[about]
        low = about.strip().lower()
        for resource, native in RESOURCES.items():
            if low in (resource.lower(), native.lower()):
                return native
        return about  # unknown/native model → pass through; the engine owns the refusal

    def resolve_attr(self, about: str, attr: str) -> str:
        return attr


class _ScopedPlane:
    """Fix round 1, C1: `nil.intent` bypassed Supplier/Customer scoping entirely, because
    `IntentResolver.resolve()` (third-party, out of adapter scope — `nilscript/dataplane/intent.py`)
    calls `self._bind.resolve_target(intent.about)` and hands the plane only the RESOLVED model
    string; the `BindingResolver` protocol has no hook to also carry a base domain. This wraps the
    real `ReadPlane` and, for every search/count/aggregate/export call whose `target` is the
    resource's native model, prepends `base_filter_for(resource)` — the exact thing `_filter_with_base`
    does for the direct `nil.search`/`nil.count`/... verbs (Task 1.1/1.2), just applied one layer up
    since this call site cannot touch the caller's filter before the resolver builds it. `get` is not
    filterable at all (same reason as `_run_nil_get`, C2), so it enforces the domain by REFUSING
    (returning `None`, the plane's own not-found shape) instead."""

    def __init__(self, plane: Any, resource: str, native: str) -> None:
        self._plane = plane
        self._resource = resource
        self._native = native

    def _scoped_filter(self, target: str, filt: Any) -> Any:
        if target != self._native:  # an unrelated/native target passed straight through — unfiltered
            return filt
        base = [
            {"field": field, "op": _TRIPLE_OP_TO_NIL[op], "value": value}
            for field, op, value in base_filter_for(self._resource)
        ]
        return [*base, *(filt or [])]

    def _plane_target(self, target: str) -> str:
        """I1 (final review): route to the RESOURCE's own name (so `describe_target` can pick its OWN
        projection — e.g. `Supplier`'s `vat`-carrying entry, packs.py) when this call's target is the
        native model we are scoping, mirroring `_target_for_plane`'s choice for the direct `nil.*`
        verbs. Only an EXACT declared resource name earns this — `IntentResolver` always resolves
        `intent.about` to `self._native` before calling us, so `self._resource` (the RAW `about`) is
        only trustworthy as a schema key when it is itself one of the declared canonical names; a case
        variant or an already-native model name keeps resolving to the plain native schema, unchanged."""
        return self._resource if target == self._native and self._resource in RESOURCES else target

    def search(self, target, *, filter, fields, limit, cursor=None, grant_fields=None):  # noqa: A002
        return self._plane.search(
            self._plane_target(target), filter=self._scoped_filter(target, filter), fields=fields,
            limit=limit, cursor=cursor, grant_fields=grant_fields,
        )

    def count(self, target, *, filter):  # noqa: A002
        return self._plane.count(self._plane_target(target), filter=self._scoped_filter(target, filter))

    def aggregate(self, target, *, filter, group_by, metrics):  # noqa: A002
        return self._plane.aggregate(
            self._plane_target(target), filter=self._scoped_filter(target, filter), group_by=group_by,
            metrics=metrics,
        )

    def export(self, target, *, filter, fields, tenant, now, approved=False, grant_fields=None):  # noqa: A002
        return self._plane.export(
            self._plane_target(target), filter=self._scoped_filter(target, filter), fields=fields,
            tenant=tenant, now=now, approved=approved, grant_fields=grant_fields,
        )

    def get(self, target, *, record_id, fields, grant_fields=None):
        # I3: `_id_satisfies_domain` no longer swallows a refused scope check into `True` — a refusal
        # here propagates to `_run_nil_intent`'s own `except _READ_REFUSALS`, never silently `None`.
        #
        # Re-review note (parked minor, not a defect): this method is UNREACHABLE from its one
        # construction site. `_run_nil_intent` builds `IntentResolver(plane, _OdooBindings())` and
        # calls `.resolve(intent)`; `IntentResolver.resolve()` (nilscript/dataplane/intent.py) only
        # ever calls `self._plane.count`/`.search`/`.aggregate` for its four `seek` shapes — it never
        # calls `.get()`, even for `seek="the"` (a single-record lookup), which goes through
        # `.search(..., limit=1)` instead. This was equally true, and equally dead, before I3 (the OLD
        # `except _READ_REFUSALS: return True` here was just as unreachable). Kept as defensive code
        # matching `_run_nil_get`'s real, exercised behavior in case a future `IntentResolver` gains a
        # `seek="the"` path that calls `.get()` directly — not because anything calls it today.
        if target == self._native and not _id_satisfies_domain(
            self._plane, self._resource, self._native, record_id
        ):
            return None
        return self._plane.get(
            self._plane_target(target), record_id=record_id, fields=fields, grant_fields=grant_fields
        )


def _run_nil_intent(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    """The single intent payload: build an Intent and resolve it deterministically over the ReadPlane.
    The caller selects no verb and builds no filter — the system owns the mechanics.

    Fix round 1, C1: a fresh `IntentResolver` is built per call (no longer cached per client) over a
    `_ScopedPlane` derived from THIS call's `about` — the resource can differ every call, so a single
    cached resolver could not carry a fixed scope. `resource`/`native` are computed with the SAME
    `_OdooBindings().resolve_target` the resolver itself calls internally on `intent.about`, so the
    proxy's notion of "the resolved model" always matches what `IntentResolver` actually passes it."""
    where = tuple(
        Binding(attr=b.get("attr"), rel=b.get("rel"), value=b.get("value"))
        for b in (args.get("where") or [])
    )
    resource = args.get("about", "")
    native = _OdooBindings().resolve_target(resource)
    plane = _ScopedPlane(_plane(client), resource, native)
    intent = Intent(
        about=resource,
        where=where,
        seek=args.get("seek", "all"),
        by=args.get("by"),
        limit=int(args.get("limit") or 50),
        cursor=args.get("cursor"),
    )
    try:
        outcome = IntentResolver(plane, _OdooBindings()).resolve(intent)
    except (
        SystemError
    ) as exc:  # an upstream (Odoo) fault is a structured refusal, never a 500
        return {"outcome": "refused", "code": "UPSTREAM_ERROR", "message": str(exc)}
    # I3 (final review): a propagated read-plane refusal (e.g. the scoped existence count behind
    # `_ScopedPlane.get` failing closed) carries its own precise code/message — surface it exactly as
    # `_run_nil_get` does, before the generic catch-all below flattens it to an undifferentiated
    # INTENT_ERROR.
    #
    # Re-review note (parked minor, not a defect): as `_ScopedPlane.get` documents, `IntentResolver`
    # never actually calls `.get()`, so THIS clause is unreachable today too — `resolve()` already
    # catches `ResultTooLarge`/`CapabilityUnsupported`/`InvalidFilter` internally
    # (nilscript/dataplane/intent.py) from its own `count`/`search`/`aggregate` calls and returns an
    # `Outcome.refusal(...)`, handled below by `outcome.kind == "refusal"` — never raises them out to
    # here. Kept for the same reason `_ScopedPlane.get`'s guard is kept: correct if `.get()` is ever
    # wired in, and free (it does not shadow or change behavior for the paths that ARE exercised).
    except _READ_REFUSALS as exc:
        return _refusal(exc)
    except Exception as exc:  # noqa: BLE001 — any resolution fault is a structured refusal, never a 500
        return {"outcome": "refused", "code": "INTENT_ERROR", "message": str(exc)}
    if outcome.kind == "refusal":
        return {"outcome": "refused", "code": outcome.code, "message": outcome.fix}
    return {"outcome": "result", "value": outcome.value}


_NIL_QUERY_VERBS: dict[str, QueryVerb] = {
    "nil.intent": QueryVerb(verb="nil.intent", run=_run_nil_intent),
    "nil.search": QueryVerb(verb="nil.search", run=_run_nil_search),
    "nil.count": QueryVerb(verb="nil.count", run=_run_nil_count),
    "nil.get": QueryVerb(verb="nil.get", run=_run_nil_get),
    "nil.aggregate": QueryVerb(verb="nil.aggregate", run=_run_nil_aggregate),
    "nil.export": QueryVerb(verb="nil.export", run=_run_nil_export),
}
# ── pack registry initialisation (must run after all verb constants are defined) ─────────────────
# packs.py imports WriteVerb/QueryVerb + reads WRITE_VERBS/QUERY_VERBS at _init_packs() call time.
# Governance is NOT imported here — governance imports DECLARED_TARGETS from this module, so
# importing it here would cycle. The pack aggregators (all_write_verbs etc.) import governance
# lazily and are only called AFTER this module finishes loading.
from odoo_nil_adapter import packs as _packs_mod  # noqa: E402

_packs_mod._init_packs()

# Reassign the public API to pack-aggregated values (behavior-preserving: all packs enabled by default,
# so the aggregate equals the former literal set). DECLARED_TARGETS stays a frozenset; governance.py
# imports it at its own load time (after translate finishes) so no cycle occurs here.
DECLARED_TARGETS = _packs_mod.all_write_targets()
WRITE_VERBS = {**_packs_mod.all_write_verbs()}
QUERY_VERBS = {**_packs_mod.all_query_verbs(), **_NIL_QUERY_VERBS, **_HISTORY_QUERY_VERBS}


# The business RESOURCES Odoo can be the system of record for, and the native model it spells each as
# (Wave A). Odoo already serves the universal read plane, so every resource here is readable; the ones
# with write verbs are fully ownable.
#
# FIX (review C3, fix round 1): `RESOURCES` stays `dict[str, str]` — the exact shape it has always
# had — because `edge.py:1236` puts it on the wire VERBATIM as `describe()["resources"]`, and
# `nilscript-controlplane`'s `resource_authority.py` (`build_target_index`, `adapter_resources`)
# already parses that field as a plain model-name string for every resource this adapter declares
# (Customer, Lead, Invoice, Payment, Product, PurchaseOrder). Changing the VALUE type there — even to
# add one new resource — is a breaking change to a live cross-repo handshake, not an adapter-local
# decision. The base domain that disambiguates `Supplier`/`Customer` lives in the SEPARATE
# `RESOURCE_DOMAINS` map below instead, which nothing outside this adapter reads.
#
# `PurchaseInvoice` is still NOT declared: Odoo spells it `account.move`, the SAME model as a customer
# invoice, and (unlike Customer/Supplier) there is no field on `account.move` that cleanly partitions
# "a purchase invoice" from "a customer invoice" the way `supplier_rank`/`customer_rank` partition
# `res.partner` — `move_type` does, but declaring it here would need the same base-domain treatment as
# Supplier below, and nothing has asked for a governed PurchaseInvoice read yet. Left undeclared on
# purpose, not by oversight: an undeclared resource still passes through as a native target
# (`account.move`) unfiltered, so nothing is lost — it just isn't offered as its own business name.
#
# `Supplier` WAS the second half of "one model, two hats" — Odoo spells both a customer and a supplier
# as `res.partner`, so a bare native target could not tell them apart, and declaring both without a
# disambiguator would make routing guess. The fix is not to guess: `res.partner` carries
# `customer_rank`/`supplier_rank` counters Odoo itself uses to mean exactly this distinction, so each
# resource declares the counter as its base domain (in `RESOURCE_DOMAINS`, not here). A `Supplier`
# read can only ever see partners with `supplier_rank > 0`; a `Customer` read keeps `supplier_rank`'s
# twin, `customer_rank > 0`. Two resources, two domains, one model — resolved by declaration, never by
# inference at read time.
RESOURCES: dict[str, str] = {
    "Customer": "res.partner",
    "Supplier": "res.partner",
    "Lead": "crm.lead",
    "Invoice": "account.move",
    "Payment": "account.payment",
    "Product": "product.product",
    "PurchaseOrder": "purchase.order",
    # Task 1.3 (D37/D38/O3): the product<->supplier link. Odoo's own model for it —
    # `product.supplierinfo` — is not shared with any other declared resource, so (unlike
    # Supplier/Customer) it needs no base-domain disambiguation; it is declared here for the same
    # reason every other resource is: a business name the agent asks about, not Odoo's own word.
    "ProductSupplier": "product.supplierinfo",
}

# The fixed base domain (Odoo triples) that scopes a resource sharing its model with another. A
# resource absent here (or present with `[]`) is read unfiltered — including every resource that was
# already declared before this task. Kept OUT of `RESOURCES` itself so the wire `describe()` field
# never changes value type (see the comment above `RESOURCES`).
#
# I5 (final review, ACCEPTED as-is): a caller that names the NATIVE model directly (`res.partner`,
# never a resource in this map) reads BOTH Customer and Supplier unfiltered — deliberately, not an
# oversight. Bypassing the business vocabulary to address Odoo's own table name is the caller's own
# choice, made with full knowledge of what that table holds; the resource base domains above exist to
# disambiguate the BUSINESS NAMES, not to retroactively police a caller who chose not to use one.
RESOURCE_DOMAINS: dict[str, list[tuple[str, str, Any]]] = {
    "Customer": [("customer_rank", ">", 0)],
    "Supplier": [("supplier_rank", ">", 0)],
}


def native_model(resource: str) -> str:
    """The native Odoo model for a declared business resource name; the value unchanged for anything
    else (a native model name, or an unknown noun the engine will refuse on its own)."""
    return RESOURCES.get(resource, resource)


def base_filter_for(resource: str) -> list[tuple[str, str, Any]]:
    """The fixed Odoo-domain triples that disambiguate a resource sharing its model with another
    (`Supplier`/`Customer` both on `res.partner`). Empty for a resource with no ambiguity to resolve,
    and for anything not declared at all — a native model name keeps today's unfiltered behaviour."""
    return list(RESOURCE_DOMAINS.get(resource, []))


def describe() -> dict[str, Any]:
    """A translate-local mirror of the wire `/nil/v0.1/describe`'s `resources` field (business name →
    native model — a plain string, exactly what `edge.describe()` has always put on the wire), for
    tests that want the declared resource vocabulary without spinning up the edge."""
    return {"resources": dict(RESOURCES)}


# ── ProductSupplier (Task 1.3, D37/D38/O3): a read-side projection over `product.supplierinfo` ────
# The curated read fields (packs.py) are Odoo's own native names (`product_tmpl_id`, `product_id`,
# `partner_id`, …) — os-server keys a link by (`sku`, `supplier_id`), which are NOT native fields.
# These helpers translate a caller's filter on the exposed names into the real ones (never sending
# Odoo a field it doesn't have), and add the exposed names onto every row the plane returns.
_PRODUCT_SUPPLIER_MODEL = "product.supplierinfo"
_PRODUCT_SUPPLIER_FILTER_ALIASES: dict[str, str] = {"supplier_id": "partner_id"}


def _resolve_sku(client: SystemClient, sku: str) -> tuple[int | None, int | None]:
    """(product_id, product_tmpl_id) for a SKU (`product.product.default_code`) — (None, None) if no
    product carries it. Never raises: an unknown sku is the CALLER's refusal to report, not this
    lookup's — the caller decides what "not found" means for its own verb."""
    if not sku:
        return None, None
    rows = client.search(
        "product.product", [["default_code", "=", sku]], fields=("id", "product_tmpl_id"), limit=1
    )
    if not rows:
        return None, None
    pid = rows[0].get("id")
    tmpl = rows[0].get("product_tmpl_id")
    if isinstance(tmpl, (list, tuple)):  # a real Odoo many2one comes back as [id, label]
        tmpl = tmpl[0] if tmpl else None
    tmpl_id = int(tmpl) if tmpl is not None else (int(pid) if pid is not None else None)
    return (int(pid) if pid is not None else None, tmpl_id)


def _unwrap_ref(value: Any) -> Any:
    """A real Odoo many2one comes back as `[id, label]`; the plain id (or scalar value), unwrapped.
    `None`-safe, and a no-op for a value that is already scalar."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _as_int_id(value: Any) -> int | None:
    unwrapped = _unwrap_ref(value)
    try:
        return int(unwrapped) if unwrapped is not None else None
    except (TypeError, ValueError):
        return None


# I2 (final review): `_lookup_default_code` did up to TWO XML-RPC round trips PER ROW (one by
# `product_id`, one falling back to `product_tmpl_id`) — for os-server's mirror poller (PAGE_LIMIT=200
# x MAX_PAGES=50), up to ~20,000 serial calls against the customer's live Odoo per sync tick. Batched:
# collect the WHOLE page's `product_id`/`product_tmpl_id` sets up front and resolve each set in ONE
# `search_read`, so a page costs at most two calls total — not two per row.
def _batch_default_codes(
    client: SystemClient, rows: list[dict[str, Any]]
) -> dict[tuple[str, int], str]:
    """`{("id", product_id) | ("product_tmpl_id", tmpl_id): default_code}` for every id referenced
    across `rows` — the batched replacement for calling `_lookup_default_code` once per row."""
    product_ids = {i for i in (_as_int_id(r.get("product_id")) for r in rows) if i is not None}
    tmpl_ids = {i for i in (_as_int_id(r.get("product_tmpl_id")) for r in rows) if i is not None}
    codes: dict[tuple[str, int], str] = {}
    if product_ids:
        for r in client.search(
            "product.product", [["id", "in", sorted(product_ids)]],
            fields=("id", "default_code"), limit=len(product_ids),
        ):
            if r.get("default_code"):
                codes[("id", int(r["id"]))] = str(r["default_code"])
    if tmpl_ids:
        # Several variants can share one template; any variant's own code stands in for the template's
        # (the original per-row lookup made the same arbitrary "first match" choice via `limit=1`,
        # unordered) — the bound below is generous, not unbounded: a page-sized set of templates times
        # a realistic variant fan-out, never "search everything".
        for r in client.search(
            "product.product", [["product_tmpl_id", "in", sorted(tmpl_ids)]],
            fields=("product_tmpl_id", "default_code"), limit=max(len(tmpl_ids) * 20, 200),
        ):
            tmpl_id = _as_int_id(r.get("product_tmpl_id"))
            if tmpl_id is not None and r.get("default_code"):
                codes.setdefault(("product_tmpl_id", tmpl_id), str(r["default_code"]))
    return codes


def _is_product_supplier(resource: str) -> bool:
    return native_model(resource) == _PRODUCT_SUPPLIER_MODEL


def _translate_product_supplier_filter(
    client: SystemClient, resource: str, filt: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """A `ProductSupplier` filter is written against the EXPOSED names (`sku`, `supplier_id`), which
    are not real `product.supplierinfo` fields — sending them to Odoo verbatim would raise "invalid
    field". `sku` resolves through a live lookup to the real `product_tmpl_id`; `supplier_id` is a
    plain rename to `partner_id`. Any other resource, or a target with no ambiguity to resolve,
    passes through unchanged."""
    if not _is_product_supplier(resource) or not filt:
        return filt
    out: list[dict[str, Any]] = []
    for clause in filt:
        field = clause.get("field")
        if field == "sku":
            _, tmpl_id = _resolve_sku(client, str(clause.get("value", "")))
            out.append({**clause, "field": "product_tmpl_id", "value": tmpl_id})
        elif field in _PRODUCT_SUPPLIER_FILTER_ALIASES:
            out.append({**clause, "field": _PRODUCT_SUPPLIER_FILTER_ALIASES[field]})
        else:
            out.append(clause)
    return out


def _project_product_supplier_row(
    client: SystemClient, resource: str, row: dict[str, Any], codes: dict[tuple[str, int], str] | None = None
) -> dict[str, Any]:
    """Add the exposed `sku`/`supplier_id` keys to one raw `product.supplierinfo` read row — never
    removing Odoo's own field names, so a caller that already reads `partner_id` keeps working.

    `codes` is the batched `{("id"|"product_tmpl_id", id): default_code}` map (I2) a page-level caller
    (`_project_product_supplier_items`) already resolved for the WHOLE page in ≤2 calls; a lone-row
    caller (`_run_nil_get`) omits it and this falls back to a one-row batch — still ≤2 calls, just not
    shared with any other row."""
    if not _is_product_supplier(resource) or not isinstance(row, dict):
        return row
    out = dict(row)
    partner = _unwrap_ref(row.get("partner_id"))
    if partner is not None:
        out["supplier_id"] = str(partner)
    lookup = codes if codes is not None else _batch_default_codes(client, [row])
    product_id = _as_int_id(row.get("product_id"))
    tmpl_id = _as_int_id(row.get("product_tmpl_id"))
    sku = lookup.get(("id", product_id)) if product_id is not None else None
    if sku is None and tmpl_id is not None:
        sku = lookup.get(("product_tmpl_id", tmpl_id))
    if sku:
        out["sku"] = sku
    return out


def _project_product_supplier_items(
    client: SystemClient, resource: str, result: dict[str, Any]
) -> dict[str, Any]:
    """The same projection, applied to every item of a `nil.search`/`nil.export`-shaped result — the
    default-code lookup is batched ONCE for the whole page (I2), never once per row."""
    if not _is_product_supplier(resource) or not isinstance(result, dict) or not result.get("items"):
        return result
    codes = _batch_default_codes(client, result["items"])
    return {
        **result,
        "items": [_project_product_supplier_row(client, resource, r, codes) for r in result["items"]],
    }


def entity_ref(verb: WriteVerb, created: dict[str, Any]) -> dict[str, Any]:
    # The SSOT entity id MUST be the backend's real record key, so a compensating delete (ROLLBACK)
    # targets the record itself — never a human attribute that can collide or change.
    rid = created.get("id") or created.get("name") or ""
    slug = verb.doctype.replace(".", "-")
    return {"type": verb.entity_type, "id": rid, "url": f"/{slug}/{rid}"}
