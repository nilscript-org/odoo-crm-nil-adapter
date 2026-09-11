"""Compensation handlers (ROLLBACK / Saga) for the Odoo CRM shim.

A reversal is a *governed* compensation: the edge previews it (PROPOSE) and executes it (COMMIT)
like any other action — never a silent write. A verb absent from `COMPENSATIONS` is IRREVERSIBLE
(the honest default): ROLLBACK of its effect REFUSES with code IRREVERSIBLE.

What's reversible here:
  • crm.create_lead    → crm.delete_lead    (REVERSIBLE — the created record is removed)
  • crm.create_contact → crm.delete_contact (REVERSIBLE)
Left IRREVERSIBLE on the semantic path: crm.update_lead_stage, crm.delete_lead, crm.delete_contact.
(For reversible field updates/deletes, use the generic `resource.update`/`resource.delete` family —
the edge captures a before-image there and synthesizes a COMPENSABLE restore/recreate.)
"""

from __future__ import annotations

from typing import Any

# verb -> reversibility + how to reverse it:
#   "verb"     : compensating verb (create -> delete) — id taken from the committed result entity.
#   "strategy" : "before_image" — the edge captures the pre-write values of the patched fields and
#                synthesizes a `resource.update` restore (COMPENSABLE, best-effort on relations).
COMPENSATIONS: dict[str, dict[str, Any]] = {
    "crm.create_lead": {"reversibility": "REVERSIBLE", "verb": "crm.delete_lead"},
    # create_contact is op=upsert: this entry is the reversal for its CREATE sub-case (no dedup match)
    # — delete the freshly created record. The MATCHED sub-case is reversed by a before-image restore
    # the edge synthesizes inline (comp_override), so it never reaches this verb-mapped delete.
    "crm.create_contact": {"reversibility": "REVERSIBLE", "verb": "crm.delete_contact"},
    # canonical Wosool vocabulary: same res.partner upsert as create_contact, same reversal.
    "crm.create_client": {"reversibility": "REVERSIBLE", "verb": "crm.delete_contact"},
    "crm.update_contact": {"reversibility": "COMPENSABLE", "strategy": "before_image"},
    # M8, the dual PO. `purchase.create_order` was IRREVERSIBLE — absent from this table — which made
    # the two-binding saga impossible to unwind: when the Daftara leg refused, there was nothing to
    # compensate the Odoo leg WITH. The order this verb creates is a DRAFT (confirming it is a
    # separate verb), and a draft purchase order unlinks cleanly; once it is confirmed or received,
    # Odoo refuses the unlink and the edge reports that refusal as a terminal failure — which is what
    # a compensation owes its caller. A reversal that cannot run is not a reversal.
    "purchase.create_order": {"reversibility": "REVERSIBLE", "verb": "purchase.delete_order"},
}

# READS ARE NOT LISTED HERE, AND THAT IS A DECISION, NOT AN OMISSION.
# `purchase.get_order_document` / `account.get_invoice_document` fetch the ERP's OWN rendered PDF for
# a record (QWeb `purchase.report_purchaseorder` / `account.report_invoice`). They write nothing, so
# there is nothing to reverse: they carry no tier and no reversibility anywhere in this adapter. In
# THIS table, absence means IRREVERSIBLE — a fail-closed default that is right for effects and wrong
# for reads. So the reads live in QUERY_VERBS (translate.py), are advertised under
# `query_verb_details` (edge.py) with an explicit `effect: "read"`, and are declared in the manifest's
# separate `read_verbs` map — never in the write-verb map whose default this table supplies. Naming
# them here (even as "IRREVERSIBLE: none") would file a non-effect in the effect plane.
NON_EFFECTING_VERBS: frozenset[str] = frozenset(
    {"purchase.get_order_document", "account.get_invoice_document"}
)

# compensating verb -> the arg that carries the real record id to act on.
_COMP_ID_ARG: dict[str, str] = {
    "crm.delete_lead": "lead_id",
    "crm.delete_contact": "contact_id",
    "purchase.delete_order": "order_id",
}


def compensate(verb: str, result: dict[str, Any]) -> dict[str, Any]:
    """Return the compensating-proposal args for `verb` given its committed `result`.

    Raises NotImplementedError for an unmapped (IRREVERSIBLE) verb — the edge turns that into an
    honest IRREVERSIBLE refusal rather than a silent corrective write.
    """
    spec = COMPENSATIONS.get(verb)
    if spec is None:
        raise NotImplementedError(f"{verb} is IRREVERSIBLE — no compensation mapped")
    comp_verb = spec["verb"]
    id_arg = _COMP_ID_ARG.get(comp_verb)
    if id_arg is None:
        raise NotImplementedError(f"no id-arg mapping for compensating verb {comp_verb}")
    entity = result.get("entity", {}) or {}
    record_id = entity.get("id") or entity.get("name") or ""
    return {id_arg: record_id}


# ── Task 1.3 (D37/D38/O3): the product<->supplier link's reversal declarations ──────────────────
# `procurement.link_supplier` / `unlink_supplier` / `link_suppliers` are NOT in WRITE_VERBS — see
# `translate.run`'s docstring for why (convergence on a COMPOUND pair + a live sku lookup, neither
# of which `edge.py`'s op=create/update/delete/method/upsert spine can express without editing
# `edge.py`, which is scaffold-generated and out of this adapter's editable scope). Because they are
# not curated write verbs, their reversal lives in this SEPARATE table rather than `COMPENSATIONS`:
# the conformance suite asserts `COMPENSATIONS`'s keyspace is exactly the verb set `edge.py` commits
# through (`test_every_declared_compensation_is_executable`, `test_manifest_declares_every_write_verb...`
# in conformance/test_purchase_order_saga.py) — adding these here would either fail that assertion
# (they compensate via a verb `edge.py` cannot run) or force a manifest entry for a verb the wire
# does not actually advertise, which would be the same false-advertising bug those gates exist to
# catch, just moved one table over.
PRODUCT_SUPPLIER_COMPENSATIONS: dict[str, dict[str, Any]] = {
    "procurement.link_supplier": {"reversibility": "REVERSIBLE", "verb": "procurement.unlink_supplier"},
    # COMPENSABLE by re-creating the exact link a before-image captured — never a blind re-run of
    # the original link_supplier args, which the caller may not have kept.
    "procurement.unlink_supplier": {
        "reversibility": "COMPENSABLE", "verb": "procurement.link_supplier", "strategy": "before_image",
    },
    # The batch's reversal unlinks EXACTLY the pairs it CREATED, never the pre-existing ones — see
    # `compensate_product_supplier_link`'s `procurement.link_suppliers` branch, which reads
    # `created_pairs` (not `linked`+`existing`) for exactly this reason. One call, not a `foreach`.
    "procurement.link_suppliers": {"reversibility": "REVERSIBLE", "verb": "procurement.unlink_suppliers"},
}


def compensate_product_supplier_link(verb: str, result: dict[str, Any]) -> dict[str, Any]:
    """The compensating-proposal args for a product<->supplier link verb — mirrors `compensate()`'s
    contract (a committed `result` in, the next call's args out) but reads `PRODUCT_SUPPLIER_COMPENSATIONS`
    instead of `COMPENSATIONS`, and builds MULTI-FIELD args (sku + supplier_id, not a single id) since
    that table's `_COMP_ID_ARG` single-scalar shape does not fit a compound identity.

    Raises NotImplementedError for an unmapped verb, matching `compensate()`'s contract."""
    spec = PRODUCT_SUPPLIER_COMPENSATIONS.get(verb)
    if spec is None:
        raise NotImplementedError(f"{verb} is IRREVERSIBLE — no compensation mapped")
    comp_verb = spec["verb"]
    if comp_verb == "procurement.unlink_supplier":
        entity = result.get("entity", {}) or {}
        return {"sku": entity.get("sku"), "supplier_id": entity.get("supplier_id")}
    if comp_verb == "procurement.link_supplier":
        # COMPENSABLE: re-create from the before-image the unlink captured, not the caller's original
        # args (an unlink call carries no price/min_qty/delay_days of its own to replay).
        before = result.get("before_image") or {}
        return {
            "sku": before.get("sku"), "supplier_id": before.get("supplier_id"),
            "price": before.get("price"), "min_qty": before.get("min_qty"),
            "delay_days": before.get("delay_days"),
        }
    if comp_verb == "procurement.unlink_suppliers":
        # REVERSIBLE: unlink exactly the pairs the batch CREATED — `created_pairs`, never `linked`+
        # `existing`'s union, which would also delete links that were already there before this call.
        return {"links": list(result.get("created_pairs") or [])}
    raise NotImplementedError(f"no compensation strategy for compensating verb {comp_verb!r}")
