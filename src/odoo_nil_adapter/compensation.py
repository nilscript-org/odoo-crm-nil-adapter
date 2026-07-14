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
