"""Compensation handlers (ROLLBACK / Saga) for the Odoo CRM shim.

A reversal is a *governed* compensation: the edge previews it (PROPOSE) and executes it (COMMIT)
like any other action — never a silent write. A verb absent from `COMPENSATIONS` is IRREVERSIBLE
(the honest default): ROLLBACK of its effect REFUSES with code IRREVERSIBLE.

What's reversible here:
  • crm.create_lead              → crm.delete_lead    (REVERSIBLE — the created record is removed)
  • crm.create_contact           → crm.delete_contact (REVERSIBLE)
  • procurement.create_supplier  → crm.delete_contact (REVERSIBLE — same res.partner, same reversal)
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
    # Task 1.3 (D37/D38/O3, fix round 1): the product<->supplier link. `procurement.unlink_supplier`
    # is intentionally ABSENT from this table (IRREVERSIBLE) — see its WriteVerb docstring in
    # translate.py for the full reasoning: edge.py's curated `op="delete"` branch never captures a
    # before-image, so a "COMPENSABLE by re-create" declaration here would be undeliverable at
    # ROLLBACK time. Only the CREATE direction is honestly reversible.
    "procurement.link_supplier": {"reversibility": "REVERSIBLE", "verb": "procurement.unlink_supplier"},
    # Task 1.3b: `procurement.create_supplier` writes the SAME `res.partner` model create_contact
    # does (only the rank field differs), so it reuses the SAME reversal — exactly the precedent
    # `crm.create_client` already set above for the identical reason ("same res.partner upsert as
    # create_contact, same reversal"). No new delete verb: `crm.delete_contact` already deletes a
    # `res.partner` by id, and it does not care which rank field put the record there.
    "procurement.create_supplier": {"reversibility": "REVERSIBLE", "verb": "crm.delete_contact"},
    # W3.6a (owner Q3): SetPrimarySupplier patches ONE field (`sequence`) on an existing link — the
    # same shape as `crm.update_contact` above. The edge captures the before-image and synthesizes a
    # `resource.update` restore; honestly COMPENSABLE, never a second copy of "was it already primary".
    "procurement.set_primary_supplier": {"reversibility": "COMPENSABLE", "strategy": "before_image"},
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
    "procurement.unlink_supplier": "link_ref",
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
