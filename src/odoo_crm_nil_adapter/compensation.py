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

# verb -> {"reversibility": "REVERSIBLE" | "COMPENSABLE", "verb": "<compensating verb>"}
COMPENSATIONS: dict[str, dict[str, Any]] = {
    "crm.create_lead": {"reversibility": "REVERSIBLE", "verb": "crm.delete_lead"},
    "crm.create_contact": {"reversibility": "REVERSIBLE", "verb": "crm.delete_contact"},
}

# compensating verb -> the arg that carries the real record id to act on.
_COMP_ID_ARG: dict[str, str] = {
    "crm.delete_lead": "lead_id",
    "crm.delete_contact": "contact_id",
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
