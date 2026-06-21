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

from odoo_crm_nil_adapter.system import SystemClient

Bilingual = dict[str, str]


@dataclass(frozen=True)
class WriteVerb:
    verb: str
    tier: str
    doctype: str  # the Odoo model this verb writes (e.g. "crm.lead", "res.partner")
    required: tuple[str, ...]
    to_native: Callable[[dict[str, Any]], dict[str, Any]]
    preview: Callable[[dict[str, Any]], Bilingual]
    entity_type: str

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


def _to_native_update_lead_stage(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → the `crm.lead` patch that moves it to another pipeline stage. `lead_id` is the
    record id (used by the edge), not a written field — only `stage_id` is patched."""
    return {"stage_id": _maybe_int(args.get("stage_id"))}


def _to_native_delete(_args: dict[str, Any]) -> dict[str, Any]:
    """delete_* verbs identify the record by their first required arg; nothing is written."""
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


WRITE_VERBS: dict[str, WriteVerb] = {
    "crm.create_lead": WriteVerb(
        verb="crm.create_lead",
        tier="MEDIUM",
        doctype="crm.lead",
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
        required=("name",),
        to_native=_to_native_create_contact,
        preview=lambda a: {
            "en": f"Create contact “{a.get('name', '')}”"
            + (f" <{a['email']}>" if a.get("email") else ""),
            "ar": f"إنشاء جهة اتصال «{a.get('name', '')}»"
            + (f" <{a['email']}>" if a.get("email") else ""),
        },
        entity_type="contact",
    ),
    "crm.update_lead_stage": WriteVerb(
        verb="crm.update_lead_stage",
        tier="MEDIUM",
        doctype="crm.lead",
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
        required=("contact_id",),
        to_native=_to_native_delete,
        preview=lambda a: {
            "en": f"Delete contact {a.get('contact_id', '')}",
            "ar": f"حذف جهة الاتصال {a.get('contact_id', '')}",
        },
        entity_type="contact",
    ),
}


QUERY_VERBS: dict[str, QueryVerb] = {
    "crm.list_leads": QueryVerb(verb="crm.list_leads", run=_run_list_leads),
    "crm.list_contacts": QueryVerb(verb="crm.list_contacts", run=_run_list_contacts),
    "crm.list_stages": QueryVerb(verb="crm.list_stages", run=_run_list_stages),
}


def entity_ref(verb: WriteVerb, created: dict[str, Any]) -> dict[str, Any]:
    # The SSOT entity id MUST be the backend's real record key, so a compensating delete (ROLLBACK)
    # targets the record itself — never a human attribute that can collide or change.
    rid = created.get("id") or created.get("name") or ""
    slug = verb.doctype.replace(".", "-")
    return {"type": verb.entity_type, "id": rid, "url": f"/{slug}/{rid}"}
