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
    write_verbs: tuple = ()  # tuple[WriteVerb, ...] — typed at runtime to avoid early import
    query_verbs: tuple = ()  # tuple[QueryVerb, ...] — same
    projections: dict = field(default_factory=dict)
    sensitive: dict = field(default_factory=dict)
    # Per-model tier overrides: {(model, op): tier}. When set, overrides the default _CRUD_TIERS
    # for specific (model, op) combos in this pack — e.g. purchase.order create is HIGH not MEDIUM.
    write_tier_overrides: dict = field(default_factory=dict)


def _make_crm_pack() -> ModulePack:
    """Build the CRM pack after translate.py verb constants are defined (avoids circular import)."""
    from odoo_nil_adapter.translate import (  # noqa: PLC0415
        CRM_CREATE_LEAD,
        CRM_CREATE_CONTACT,
        CRM_UPDATE_CONTACT,
        CRM_LOG_NOTE,
        CRM_UPDATE_LEAD_STAGE,
        CRM_DELETE_LEAD,
        CRM_DELETE_CONTACT,
        CRM_LIST_LEADS,
        CRM_LIST_CONTACTS,
        CRM_LIST_STAGES,
        CRM_LIST_COUNTRIES,
        CRM_GET_CONTACT_BY_PHONE,
        CRM_FIND_CONTACT,
        CRM_GET_CONTACT,
        WOSOOL_CREATE_CLIENT,
    )

    return ModulePack(
        name="crm",
        model_prefixes=(
            "crm.",
            "res.partner",
            "res.country",
            "res.country.state",
            "res.partner.category",
        ),
        write_targets=(
            "crm.lead",
            "res.partner",
            "crm.stage",
            "crm.tag",
            "res.partner.category",
            "crm.team",
            "res.country",
            "res.country.state",
        ),
        method_grants=(
            ("res.partner", "message_post", "MEDIUM", None),
            ("crm.lead", "message_post", "MEDIUM", None),
        ),
        write_verbs=(
            CRM_CREATE_LEAD,
            CRM_CREATE_CONTACT,
            CRM_UPDATE_CONTACT,
            CRM_LOG_NOTE,
            CRM_UPDATE_LEAD_STAGE,
            CRM_DELETE_LEAD,
            CRM_DELETE_CONTACT,
            WOSOOL_CREATE_CLIENT,  # canonical Wosool vocabulary (baseline catalog)
        ),
        query_verbs=(
            CRM_LIST_LEADS,
            CRM_LIST_CONTACTS,
            CRM_LIST_STAGES,
            CRM_LIST_COUNTRIES,
            CRM_GET_CONTACT_BY_PHONE,
            CRM_FIND_CONTACT,
            CRM_GET_CONTACT,
        ),
        projections={
            "res.partner": ("id", "name", "phone", "email"),
            "crm.lead": (
                "id",
                "name",
                "contact_name",
                "email_from",
                "phone",
                "stage_id",
                "expected_revenue",
            ),
            "crm.stage": ("id", "name", "sequence"),
            "crm.team": ("id", "name"),
            "res.country": ("id", "name", "code"),
        },
        sensitive={"res.partner": frozenset({"credit_limit", "vat"})},
    )


def _make_finance_pack() -> ModulePack:
    from odoo_nil_adapter.translate import (  # noqa: PLC0415
        ACCOUNT_CREATE_INVOICE,
        ACCOUNT_GET_INVOICE_DOCUMENT,
        ACCOUNT_POST_INVOICE,
        ACCOUNT_REGISTER_PAYMENT,
        WOSOOL_CREATE_INVOICE,
        WOSOOL_RECORD_PAYMENT,
        WOSOOL_CREATE_PURCHASE_INVOICE,
    )

    return ModulePack(
        name="finance",
        model_prefixes=("account.",),
        write_targets=(),  # finance writes stay grant-only — not in default skeleton
        method_grants=(),
        write_verbs=(
            ACCOUNT_CREATE_INVOICE,
            ACCOUNT_POST_INVOICE,
            ACCOUNT_REGISTER_PAYMENT,
            # canonical Wosool vocabulary (baseline catalog): services/commerce/procurement
            # names mapped to the real Odoo documents (out_invoice / payment / in_invoice)
            WOSOOL_CREATE_INVOICE,
            WOSOOL_RECORD_PAYMENT,
            WOSOOL_CREATE_PURCHASE_INVOICE,
        ),
        # A READ: the ERP's OWN rendered invoice / vendor bill (QWeb `account.report_invoice`).
        query_verbs=(ACCOUNT_GET_INVOICE_DOCUMENT,),
        projections={
            "account.move": (
                "id",
                "name",
                "ref",
                "state",
                "move_type",
                "partner_id",
                "invoice_date",
                "invoice_date_due",
                "amount_total",
                "amount_residual",
                "currency_id",
                "journal_id",
            ),
            "account.payment": (
                "id",
                "name",
                "state",
                "payment_type",
                "partner_id",
                "amount",
                "currency_id",
                "journal_id",
                "date",
                "ref",
            ),
            "account.move.line": (
                "id",
                "name",
                "move_id",
                "account_id",
                "partner_id",
                "debit",
                "credit",
                "balance",
                "date",
                "quantity",
                "price_unit",
            ),
            "account.journal": (
                "id",
                "name",
                "code",
                "type",
                "currency_id",
                "company_id",
            ),
            "account.account": (
                "id",
                "name",
                "code",
                "account_type",
                "reconcile",
                "currency_id",
            ),
            "account.tax": (
                "id",
                "name",
                "amount",
                "amount_type",
                "type_tax_use",
                "company_id",
            ),
        },
        sensitive={},
    )


def _make_sales_pack() -> ModulePack:
    from odoo_nil_adapter.translate import SALE_CONFIRM_ORDER  # noqa: PLC0415

    return ModulePack(
        name="sales",
        model_prefixes=("sale.",),
        write_targets=(),
        method_grants=(),
        write_verbs=(SALE_CONFIRM_ORDER,),
        query_verbs=(),
        projections={},
        sensitive={},
    )


def _make_inventory_pack() -> ModulePack:
    from odoo_nil_adapter.translate import (  # noqa: PLC0415
        STOCK_VALIDATE_PICKING,
        WOSOOL_CREATE_PRODUCT,
    )

    return ModulePack(
        name="inventory",
        model_prefixes=("stock.", "product."),
        write_targets=(),
        method_grants=(),
        write_verbs=(STOCK_VALIDATE_PICKING, WOSOOL_CREATE_PRODUCT),
        query_verbs=(),
        projections={},
        sensitive={},
    )


def _make_purchasing_pack() -> ModulePack:
    from odoo_nil_adapter.translate import (  # noqa: PLC0415
        PURCHASE_CREATE_ORDER,
        PURCHASE_CONFIRM_ORDER,
        PURCHASE_DELETE_ORDER,
        PURCHASE_GET_ORDER_DOCUMENT,
        WOSOOL_SET_LANDED_COST,
    )

    return ModulePack(
        name="purchasing",
        model_prefixes=("purchase.", "uom."),
        write_targets=("purchase.order",),
        method_grants=(("purchase.order", "button_confirm", "HIGH", "button_cancel"),),
        write_verbs=(
            PURCHASE_CREATE_ORDER,
            # the inverse of the create — without it the Odoo PO leg of the dual PO is unreversible,
            # and the saga cannot unwind when the Daftara leg refuses.
            PURCHASE_DELETE_ORDER,
            PURCHASE_CONFIRM_ORDER,
            WOSOOL_SET_LANDED_COST,
        ),
        # A READ: the ERP's OWN rendered purchase order (QWeb `purchase.report_purchaseorder`). The
        # document the vendor receives is Odoo's, never one we compose from the record.
        query_verbs=(PURCHASE_GET_ORDER_DOCUMENT,),
        projections={
            "purchase.order": (
                "id",
                "name",
                "partner_id",
                "date_order",
                "date_planned",
                "state",
                "origin",
                "currency_id",
                "amount_total",
            ),
            "purchase.order.line": (
                "id",
                "name",
                "order_id",
                "product_id",
                "product_qty",
                "product_uom",
                "price_unit",
                "date_planned",
            ),
            "product.product": (
                "id",
                "default_code",
                "name",
                "barcode",
                "uom_id",
                "uom_po_id",
                "list_price",
                "standard_price",
            ),
            "stock.quant": ("id", "product_id", "location_id", "quantity"),
        },
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
        _make_purchasing_pack(),
    )
    _packs_initialized = True


def enabled_packs() -> tuple[ModulePack, ...]:
    from odoo_nil_adapter import governance  # noqa: PLC0415

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


def all_write_tier_overrides() -> dict:
    """Merged per-(model,op) tier overrides from all enabled packs."""
    out: dict = {}
    for p in enabled_packs():
        out.update(p.write_tier_overrides)
    return out


def module_models() -> dict[str, tuple[str, ...]]:
    """Map pack.name -> pack.model_prefixes for all registered packs."""
    return {p.name: p.model_prefixes for p in PACKS}


def default_methods() -> dict:
    """Build a {(model, method): MethodGrant} dict from enabled packs' method_grants."""
    from odoo_nil_adapter.governance import MethodGrant  # noqa: PLC0415

    out: dict = {}
    for p in enabled_packs():
        for model, method, tier, reverse in p.method_grants:
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
