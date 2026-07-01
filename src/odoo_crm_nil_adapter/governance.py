"""Governance policy — the declarative, data-driven home of the adapter's write/method/scope ceiling.

Doctrine (Odoo full coverage): lifting the READ ceiling does NOT lift the WRITE ceiling.

  • Reads went universal — `nil.*` projects/sensitivity-gates ANY discovered model (read_plane.py).
  • Writes and workflow methods stay GOVERNED here, as policy *data*:
      - Phase 2 — a per-(model, op) tier table with safe defaults; DESTRUCTIVE ops on financial/HR models
        escalate to CRITICAL (owner-only); a per-tenant grant overlay widens the surface for one tenant
        (onboarding) without code changes or cross-tenant leakage.
      - Phase 3 — methods carry a tier AND an optional reverse method (post→cancel), so a workflow action
        can be COMPENSABLE instead of always IRREVERSIBLE.
      - Phase 5 — an operator can enable only some module GROUPS; everything outside scope is unexpressible
        (discovery + writes both consult `module_enabled`).

Ungranted ⇒ default-deny everywhere, so the structural-unexpressibility invariant (β⁻¹(a)=∅ for an
undeclared write) holds by construction — see test_unexpressibility.py.

Pure-decision module: no I/O, no Odoo specifics. The edge/read-plane consult it; widening the surface is
editing this table (or a per-tenant grant), never loosening a hardcoded check elsewhere.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# ── model classification (drives sensitivity + destructive escalation) ────────────────────────────
_CLASS_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("financial", ("account.",)),
    ("hr", ("hr.",)),
    ("system", ("res.users", "res.groups", "res.company", "ir.")),
)


def model_class(target: str) -> str:
    """Coarse risk class of an Odoo model — `financial` / `hr` / `system` / `general`. Drives the
    destructive-op escalation and the read-plane's sensitivity defaults."""
    for cls, prefixes in _CLASS_PREFIXES:
        if any(target == p.rstrip(".") or target.startswith(p) for p in prefixes):
            return cls
    return "general"


# ── module groups (Phase 5 scoping) ───────────────────────────────────────────────────────────────
# group -> model prefixes/exact names it owns. An operator enables a SUBSET; everything else is out of
# scope. CRM keeps the reference models the curated verbs resolve against so the existing surface holds.
_MODULE_MODELS: dict[str, tuple[str, ...]] = {
    "crm": ("crm.", "res.partner", "res.country", "res.country.state", "res.partner.category"),
    "sales": ("sale.",),
    "finance": ("account.",),
    "inventory": ("stock.", "product."),
    "hr": ("hr.",),
    "manufacturing": ("mrp.",),
    "project": ("project.",),
}


# ── tiers ─────────────────────────────────────────────────────────────────────────────────────────
_CRUD_TIERS: dict[str, str] = {"create": "MEDIUM", "update": "MEDIUM", "delete": "HIGH"}


@dataclass(frozen=True)
class MethodGrant:
    tier: str
    reverse: str | None = None  # the inverse method (post→button_draft, confirm→action_cancel), or None


# Lazy getters — computed on first call to avoid a circular import.
# (translate.py late-imports packs at its bottom; packs.py imports verb constants from translate;
# governance must NOT import DECLARED_TARGETS at module level or the cycle breaks.)
def _get_declared_targets() -> frozenset[str]:
    from odoo_crm_nil_adapter.translate import DECLARED_TARGETS  # lazy
    return DECLARED_TARGETS


def _get_default_write() -> dict[tuple[str, str], str]:
    return {(m, op): tier for m in _get_declared_targets() for op, tier in _CRUD_TIERS.items()}


def _get_default_method() -> dict[tuple[str, str], MethodGrant]:
    """Default method allow-list (default-deny). Derived from enabled pack method_grants."""
    from odoo_crm_nil_adapter import packs as _p  # noqa: PLC0415
    return _p.default_methods()

# ── mutable policy state (per-tenant grants + operator module scope) ──────────────────────────────
# Thin seams for onboarding/SaaS — populated by the operator or a future provisioning call. Empty by
# default, so a fresh instance behaves exactly like the shipped skeleton.
_TENANT_WRITE: dict[str, dict[tuple[str, str], str]] = {}
_TENANT_METHOD: dict[str, dict[tuple[str, str], MethodGrant]] = {}
_ENABLED_MODULES: set[str] | None = None  # None = every module enabled (single-instance default)


def reset_policy() -> None:
    """Drop all per-tenant grants and module scope — back to shipped safe defaults (used by tests)."""
    global _ENABLED_MODULES
    _TENANT_WRITE.clear()
    _TENANT_METHOD.clear()
    _ENABLED_MODULES = None


def grant_write(tenant: str, target: str, op: str, tier: str) -> None:
    """Deliberately widen one tenant's writable surface (onboarding) — escalation still applies."""
    _TENANT_WRITE.setdefault(tenant, {})[(target, op)] = tier


def grant_method(tenant: str, target: str, method: str, tier: str, *, reverse: str | None = None) -> None:
    _TENANT_METHOD.setdefault(tenant, {})[(target, method)] = MethodGrant(tier, reverse)


def set_enabled_modules(modules: set[str] | None) -> None:
    """Operator scope: enable only these module groups (None = all). Out-of-scope models are
    undiscoverable AND unwritable."""
    global _ENABLED_MODULES
    _ENABLED_MODULES = set(modules) if modules is not None else None


def load_grants_from_env(var: str = "NIL_TENANT_GRANTS") -> int:
    """Persist per-tenant grants from a config var (PERSISTENCE: survives restart, set by onboarding).
    JSON shape:
      {"ws_acme": {"writes":  [{"model":"account.move","op":"create","tier":"HIGH"}],
                   "methods": [{"model":"account.move","method":"action_post","tier":"HIGH",
                                "reverse":"button_draft"}]}}
    Returns the count of grants applied (0 if the var is unset/malformed — fail-safe, never raises)."""
    raw = os.environ.get(var)
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return 0
    n = 0
    for tenant, spec in (data or {}).items():
        for w in spec.get("writes", []):
            grant_write(tenant, w["model"], w["op"], w.get("tier", "MEDIUM"))
            n += 1
        for m in spec.get("methods", []):
            grant_method(tenant, m["model"], m["method"], m.get("tier", "MEDIUM"), reverse=m.get("reverse"))
            n += 1
    return n


# ── resolution ─────────────────────────────────────────────────────────────────────────────────────
def module_enabled(target: str) -> bool:
    """Is this model within the operator-enabled module scope? True when no scope is set."""
    if _ENABLED_MODULES is None:
        return True
    from odoo_crm_nil_adapter import packs as _p  # noqa: PLC0415
    live_models = {**_p.module_models(), **_MODULE_MODELS}
    for group in _ENABLED_MODULES:
        if any(target == p.rstrip(".") or target.startswith(p) for p in live_models.get(group, ())):
            return True
    return False


def _escalate(target: str, op: str, tier: str) -> str:
    """A destructive op (delete) on a financial/HR model is owner-only CRITICAL, regardless of the
    granted tier — the one-way doors (unlink an invoice / a payslip) never auto-execute."""
    if op == "delete" and model_class(target) in ("financial", "hr"):
        return "CRITICAL"
    return tier


def write_tier(target: str, op: str, *, tenant: str | None = None) -> str | None:
    """Tier for a generic resource.* write, or None = default-deny. Consults the per-tenant overlay
    first, then the shipped skeleton; an out-of-scope model is denied even if granted."""
    if not module_enabled(target):
        return None
    grant = (_TENANT_WRITE.get(tenant or "", {}).get((target, op))) or _get_default_write().get((target, op))
    if grant is None:
        return None
    return _escalate(target, op, grant)


def _method_grant(target: str, method: str, tenant: str | None) -> MethodGrant | None:
    return (_TENANT_METHOD.get(tenant or "", {}).get((target, method))) or _get_default_method().get((target, method))


def method_tier(target: str, method: str, *, tenant: str | None = None) -> str | None:
    """Tier for a generic workflow method, or None = default-deny. Only allow-listed (model, method)
    pairs are committable; an out-of-scope model is denied even if granted."""
    if not module_enabled(target):
        return None
    grant = _method_grant(target, method, tenant)
    return grant.tier if grant is not None else None


def method_reverse(target: str, method: str, *, tenant: str | None = None) -> str | None:
    """The inverse method for a granted workflow method (post→button_draft), or None = IRREVERSIBLE."""
    grant = _method_grant(target, method, tenant)
    return grant.reverse if grant is not None else None


def reads_allowed_raw(target: str) -> bool:
    """Legacy raw `resource.read` (whole-record list, NOT projected) stays bounded to the writable
    skeleton — universal reads flow through the projected, sensitivity-gated `nil.*` plane, so
    relaxing discovery never exposes a raw salary/VAT dump via the old list endpoint."""
    return module_enabled(target) and target in _get_declared_targets()


def writable_targets() -> list[str]:
    """The committable WRITE skeleton describe() advertises (in-scope granted models)."""
    return sorted({m for (m, _op) in _get_default_write() if module_enabled(m)})
