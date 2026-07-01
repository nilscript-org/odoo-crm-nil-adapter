"""Per-module verb packs: the single place each Odoo module declares what it contributes —
curated write/query verbs, its default write skeleton, method grants, module-group prefixes,
and read projections. translate.py / governance.py / read_plane.py aggregate over ENABLED packs
instead of hardcoding tables. A pack's `name` IS its governance module group."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModulePack:
    name: str
    model_prefixes: tuple[str, ...]
    write_targets: tuple[str, ...] = ()
    method_grants: tuple[tuple[str, str, str, str | None], ...] = ()
    write_verbs: tuple = ()   # tuple[WriteVerb, ...] — typed at runtime to avoid early import
    query_verbs: tuple = ()   # tuple[QueryVerb, ...] — same
    projections: dict = field(default_factory=dict)
    sensitive: dict = field(default_factory=dict)


def _make_crm_pack() -> ModulePack:
    """Build the CRM pack after translate.py verb constants are defined (avoids circular import)."""
    from odoo_crm_nil_adapter.translate import (  # noqa: PLC0415
        WRITE_VERBS as _WV,
        QUERY_VERBS as _QV,
    )
    # Pull verb objects from the pre-packs literal dicts built in translate.py.
    # These exist at the module level BEFORE the bottom-of-file aggregation runs.
    write_verbs = tuple(
        v for k, v in _WV.items()
        if k in (
            "crm.create_lead", "crm.create_contact", "crm.update_contact", "crm.log_note",
            "crm.update_lead_stage", "crm.delete_lead", "crm.delete_contact",
        )
    )
    query_verbs = tuple(
        v for k, v in _QV.items()
        if k in (
            "crm.list_leads", "crm.list_contacts", "crm.list_stages",
            "crm.list_countries", "crm.get_contact_by_phone",
        )
    )
    return ModulePack(
        name="crm",
        model_prefixes=("crm.", "res.partner", "res.country", "res.country.state", "res.partner.category"),
        write_targets=(
            "crm.lead", "res.partner", "crm.stage", "crm.tag",
            "res.partner.category", "crm.team", "res.country", "res.country.state",
        ),
        method_grants=(
            ("res.partner", "message_post", "MEDIUM", None),
            ("crm.lead", "message_post", "MEDIUM", None),
        ),
        write_verbs=write_verbs,
        query_verbs=query_verbs,
        projections={
            "res.partner": ("id", "name", "phone", "email"),
            "crm.lead": ("id", "name", "contact_name", "email_from", "phone", "stage_id", "expected_revenue"),
            "crm.stage": ("id", "name", "sequence"),
            "crm.team": ("id", "name"),
            "res.country": ("id", "name", "code"),
        },
        sensitive={"res.partner": frozenset({"credit_limit", "vat"})},
    )


def _make_finance_pack() -> ModulePack:
    from odoo_crm_nil_adapter.translate import WRITE_VERBS as _WV  # noqa: PLC0415
    write_verbs = tuple(
        v for k, v in _WV.items()
        if k in ("account.create_invoice", "account.post_invoice", "account.register_payment")
    )
    return ModulePack(
        name="finance",
        model_prefixes=("account.",),
        write_targets=(),  # finance writes stay grant-only — not in default skeleton
        method_grants=(),
        write_verbs=write_verbs,
        query_verbs=(),
        projections={
            "account.move": ("id", "name", "ref", "state", "move_type", "partner_id", "invoice_date",
                             "invoice_date_due", "amount_total", "amount_residual", "currency_id", "journal_id"),
            "account.payment": ("id", "name", "state", "payment_type", "partner_id", "amount", "currency_id",
                                "journal_id", "date", "ref"),
            "account.move.line": ("id", "name", "move_id", "account_id", "partner_id", "debit", "credit",
                                  "balance", "date", "quantity", "price_unit"),
            "account.journal": ("id", "name", "code", "type", "currency_id", "company_id"),
            "account.account": ("id", "name", "code", "account_type", "reconcile", "currency_id"),
            "account.tax": ("id", "name", "amount", "amount_type", "type_tax_use", "company_id"),
        },
        sensitive={},
    )


def _make_sales_pack() -> ModulePack:
    from odoo_crm_nil_adapter.translate import WRITE_VERBS as _WV  # noqa: PLC0415
    write_verbs = tuple(v for k, v in _WV.items() if k == "sale.confirm_order")
    return ModulePack(
        name="sales",
        model_prefixes=("sale.",),
        write_targets=(),
        method_grants=(),
        write_verbs=write_verbs,
        query_verbs=(),
        projections={},
        sensitive={},
    )


def _make_inventory_pack() -> ModulePack:
    from odoo_crm_nil_adapter.translate import WRITE_VERBS as _WV  # noqa: PLC0415
    write_verbs = tuple(v for k, v in _WV.items() if k == "stock.validate_picking")
    return ModulePack(
        name="inventory",
        model_prefixes=("stock.", "product."),
        write_targets=(),
        method_grants=(),
        write_verbs=write_verbs,
        query_verbs=(),
        projections={},
        sensitive={},
    )


# PACKS is populated lazily on first access to avoid import-time circular dependency.
# translate.py defines verb constants, then at the bottom calls _init_packs() which populates
# this module's PACKS tuple.
PACKS: tuple[ModulePack, ...] = ()
_packs_initialized = False


def _init_packs() -> None:
    """Called from translate.py after all verb constants are defined. Populates PACKS."""
    global PACKS, _packs_initialized
    if _packs_initialized:
        return
    PACKS = (
        _make_crm_pack(),
        _make_finance_pack(),
        _make_sales_pack(),
        _make_inventory_pack(),
    )
    _packs_initialized = True


def enabled_packs() -> tuple[ModulePack, ...]:
    from odoo_crm_nil_adapter import governance  # noqa: PLC0415
    return tuple(p for p in PACKS if governance.module_enabled(p.name))


def all_write_verbs() -> dict:
    out: dict = {}
    for p in enabled_packs():
        for v in p.write_verbs:
            out[v.verb] = v
    return out


def all_query_verbs() -> dict:
    out: dict = {}
    for p in enabled_packs():
        for v in p.query_verbs:
            out[v.verb] = v
    return out


def all_write_targets() -> frozenset:
    targets: set = set()
    for p in enabled_packs():
        targets.update(p.write_targets)
    return frozenset(targets)


def module_models() -> dict[str, tuple[str, ...]]:
    """Map pack.name -> pack.model_prefixes for all registered packs."""
    return {p.name: p.model_prefixes for p in PACKS}


def default_methods() -> dict:
    """Build a {(model, method): MethodGrant} dict from enabled packs' method_grants."""
    from odoo_crm_nil_adapter.governance import MethodGrant  # noqa: PLC0415
    out: dict = {}
    for p in enabled_packs():
        for (model, method, tier, reverse) in p.method_grants:
            out[(model, method)] = MethodGrant(tier, reverse)
    return out


def all_projections() -> dict[str, tuple[str, ...]]:
    """Merged projection map from all enabled packs."""
    out: dict[str, tuple[str, ...]] = {}
    for p in enabled_packs():
        out.update(p.projections)
    return out


def all_sensitive() -> dict[str, frozenset]:
    """Merged sensitivity map from all enabled packs."""
    out: dict[str, frozenset] = {}
    for p in enabled_packs():
        out.update(p.sensitive)
    return out
