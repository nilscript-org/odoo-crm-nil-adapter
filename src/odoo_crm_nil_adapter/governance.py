"""Governance policy — the declarative, data-driven home of the adapter's write/method ceiling.

Phase-1 doctrine (Odoo full coverage): lifting the READ ceiling does NOT lift the WRITE ceiling.

  • Reads went universal — `nil.*` projects/sensitivity-gates ANY discovered model (read_plane.py).
  • Writes and workflow methods stay GOVERNED here. The old hardcoded `DECLARED_TARGETS` frozenset
    becomes policy *data*: only models the policy grants are committable; financial/HR and every other
    discovered model are unexpressible for writes until DELIBERATELY widened. So the structural
    invariant (β⁻¹(a)=∅ for an undeclared write) still holds — see test_unexpressibility.py.

This is a pure-decision module: no I/O, no Odoo specifics. The edge consults it; widening the surface
is editing this table (later: per-tenant overrides), never loosening a hardcoded check in the edge.
"""

from __future__ import annotations

from odoo_crm_nil_adapter.translate import DECLARED_TARGETS

# Generic resource.* CRUD tiers. delete is HIGH (destructive); create/update MEDIUM. A model absent
# from the writable skeleton returns None from `write_tier` → default-deny, refused at PROPOSE.
_CRUD_TIERS: dict[str, str] = {"create": "MEDIUM", "update": "MEDIUM", "delete": "HIGH"}

# The writable skeleton — the models generic resource.* may commit. Phase 1 keeps this identical to the
# legacy declared set (CRM domain); the value of moving it here is that it is now widenable data and a
# single seam the method policy shares. Widen per (model, op) deliberately in later phases.
WRITABLE_TARGETS: frozenset[str] = DECLARED_TARGETS

# Per-(model, method) allow-list for the generic `op="method"` workflow path. DEFAULT-DENY: a method
# not listed here is refused — no arbitrary model method is callable. `message_post` (append a chatter
# note) is broadly safe, so it is granted on the CRM write models. Financial/HR workflow methods
# (account.move.action_post, hr.payslip.*) are intentionally ABSENT — they stay unexpressible until a
# later phase grants them with the right tier and reversibility.
_METHOD_POLICY: dict[tuple[str, str], str] = {
    ("res.partner", "message_post"): "MEDIUM",
    ("crm.lead", "message_post"): "MEDIUM",
}


def write_tier(target: str, op: str) -> str | None:
    """Tier for a generic resource.* write (`create`/`update`/`delete`), or None = default-deny.
    None means the (target, op) is unexpressible for writes — the edge refuses it at PROPOSE."""
    if target in WRITABLE_TARGETS:
        return _CRUD_TIERS.get(op)
    return None


def method_tier(target: str, method: str) -> str | None:
    """Tier for a generic workflow method on a model, or None = default-deny. Only allow-listed
    (model, method) pairs are committable — an unlisted method is refused, never invoked."""
    return _METHOD_POLICY.get((target, method))


def reads_allowed_raw(target: str) -> bool:
    """Legacy raw `resource.read` (whole-record list, NOT projected) stays bounded to the writable
    skeleton — universal reads flow through the projected, sensitivity-gated `nil.*` plane, so
    relaxing discovery never exposes a raw salary/VAT dump via the old list endpoint."""
    return target in WRITABLE_TARGETS
