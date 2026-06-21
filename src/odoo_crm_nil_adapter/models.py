"""Pydantic models GENERATED from the NIL standard's JSON-Schema arg profiles.

Do NOT edit by hand — regenerate with `nilscript scaffold-shim`. One model per ACTIVE verb;
deprecated/parked verbs are intentionally absent (the standard says do not implement them).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CommerceCreateCouponArgs(BaseModel):
    """commerce.create_coupon args. commerce.create_coupon@1.0.0 args"""
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=2)
    discount_type: Any
    discount_value: float = Field(..., gt=0, description='Checked against the workspace discount limit (§6.4 amount thresholds).')
    expiry_date: str | None = None
    usage_limit: int | None = Field(None, ge=1)

class CommerceCreateProductArgs(BaseModel):
    """commerce.create_product args. commerce.create_product@1.2.0 args"""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    description: str | None = None
    category: str | None = Field(None, description='Resolvable fact: name or id; resolved server-side (§6.3).')
    images: list[str] | None = None
    price: float | None = Field(None, gt=0, description='Flat single-variant sugar (D-1). Mutually exclusive with `variants` (see oneOf).')
    sku: str | None = None
    sale_price: float | None = Field(None, gt=0)
    quantity: int | None = Field(None, ge=0)
    options: list[dict[str, Any]] | None = Field(None, description='Single-variant option matrix. D-1 DEEPEST APPLICATION: array→object→array→object (level 2). No level beyond this is admissible anywhere in the standard.')
    variants: list[dict[str, Any]] | None = Field(None, description='Explicit decomposition for platforms that split product/variant (GAP-003). D-1 level-2: array of objects, each containing at most one flat array (option_values). Mutually exclusive with the flat price/sku/sale_price/quantity/options.')

class CommerceDeleteProductArgs(BaseModel):
    """commerce.delete_product args. commerce.delete_product@1.0.0 args — DESTRUCTIVE (floor HIGH; Grant must name this verb explicitly)"""
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(..., description='Resolvable fact (§6.3).')
    reason: str | None = Field(None, description='Audited; SHOULD be required by workspace rule.')

class CommerceProcessRefundArgs(BaseModel):
    """commerce.process_refund args. commerce.process_refund@2.0.0 args — floor HIGH; amount resolved from the target of record, never from args"""
    model_config = ConfigDict(extra="forbid")

    refund_target: dict[str, Any] = Field(..., description='The unit being refunded, named abstractly (D-1 level-1 object). Each System accepts the type(s) it natively models and refuses the rest with UNRESOLVED/INVALID_ARGS — never a transport error.')
    reason: str | None = None

class CommerceRecordFulfillmentArgs(BaseModel):
    """commerce.record_fulfillment args. commerce.record_fulfillment@1.0.0 args — records a fulfillment FACT; the System derives status"""
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., description='Resolvable fact (§6.3). Aliases: orderId, order_number, reference_id.')
    event: Any = Field(..., description="The fulfillment fact being recorded. The System derives the order's fulfillment/lifecycle status from recorded facts; this verb never sets a status field (GAP-001, versions/0.2.0.md).")
    items: list[dict[str, Any]] | None = Field(None, description='Optional per-line scope (D-1 level-1: array of scalar objects).')
    carrier: str | None = None
    tracking: str | None = None
    occurred_at: str | None = Field(None, description='RFC 3339; when the fact occurred, if not now.')

class CommerceRecordPaymentArgs(BaseModel):
    """commerce.record_payment args. commerce.record_payment@1.0.0 args — floor HIGH; records a payment FACT; the System derives financial status"""
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., description='Resolvable fact (§6.3).')
    event: Any = Field(..., description="The payment fact. The System derives the order's financial status; this verb never sets a status field (GAP-001).")
    amount: float | None = Field(None, gt=0, description='Optional hint; the System resolves the authoritative amount from the order/payment of record (§6.3). A Speaker-declared amount never overrides it.')
    currency: str | None = Field(None, pattern='^[A-Z]{3}$', description='ISO 4217.')
    method: str | None = None
    reference: str | None = Field(None, description='Processor/transaction reference.')
    occurred_at: str | None = None

class CommerceSendMessageArgs(BaseModel):
    """commerce.send_message args. commerce.send_message@1.0.0 args — floor HIGH; phone+text redacted from telemetry"""
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(..., description='Resolvable fact: normalized E.164 + consent check server-side. Aliases: mobile, msisdn, to.')
    text: str = Field(..., min_length=1)

class CommerceUpdateProductArgs(BaseModel):
    """commerce.update_product args. commerce.update_product@1.0.0 args"""
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(..., description='Resolvable fact: id, name, 1-indexed position, or server-minted reference (§6.3).')
    updates: dict[str, Any]

class CommerceUpdateProductQuantityArgs(BaseModel):
    """commerce.update_product_quantity args. commerce.update_product_quantity@1.0.0 args"""
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(..., description='Resolvable fact (§6.3).')
    quantity: int = Field(..., ge=0)
    mode: Any | None = None

class ResourceCreateArgs(BaseModel):
    """resource.create args. resource.create@1.0.0 args"""
    model_config = ConfigDict(extra="forbid")

    target: str
    data: dict[str, Any]

class ResourceDeleteArgs(BaseModel):
    """resource.delete args. resource.delete@1.0.0 args"""
    model_config = ConfigDict(extra="forbid")

    target: str
    id: str

class ResourceReadArgs(BaseModel):
    """resource.read args. resource.read@1.0.0 args"""
    model_config = ConfigDict(extra="forbid")

    target: str
    match: dict[str, Any] | None = None

class ResourceUpdateArgs(BaseModel):
    """resource.update args. resource.update@1.0.0 args"""
    model_config = ConfigDict(extra="forbid")

    target: str = Field(..., description='native target (collection/table/object)')
    id: str = Field(..., description='record id or human identifier')
    data: dict[str, Any] = Field(..., description='fields to change')

class ServicesCreateClientArgs(BaseModel):
    """services.create_client args. services.create_client args"""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    phone: str = Field(..., description='Normalized to E.164 server-side (resolvable fact).')
    email: str | None = None

class ServicesCreateInvoiceArgs(BaseModel):
    """services.create_invoice args. services.create_invoice args — floor HIGH; VAT computed by the System"""
    model_config = ConfigDict(extra="forbid")

    party_id: str = Field(..., description='Resolvable fact (§6.3).')
    amount: float = Field(..., gt=0)
    currency: str = Field(..., pattern='^[A-Z]{3}$')
    description: str | None = None

class ServicesCreatePaymentLinkArgs(BaseModel):
    """services.create_payment_link args. services.create_payment_link args — floor HIGH; invoice must exist; PSP-licensed link only"""
    model_config = ConfigDict(extra="forbid")

    invoice_id: str = Field(..., description='Resolvable fact: amount and provider derive from the stored invoice (§6.3).')

class ServicesDraftProposalArgs(BaseModel):
    """services.draft_proposal args. services.draft_proposal args"""
    model_config = ConfigDict(extra="forbid")

    party_id: str = Field(..., description='Resolvable fact: must exist (§6.3).')
    title: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)
    currency: str = Field(..., pattern='^[A-Z]{3}$')
    body: str | None = None

class ServicesListClientsArgs(BaseModel):
    """services.list_clients args. services.list_clients@1.0.0 args — read-only QUERY; no side effects"""
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, description='Optional name filter; locale-folded match (§6.3 rule 5).')

class ServicesSendFollowupArgs(BaseModel):
    """services.send_followup args. services.send_followup args — consent must be active (resolved server-side)"""
    model_config = ConfigDict(extra="forbid")

    party_id: str = Field(..., description='Resolvable fact; consent state checked server-side (§6.3).')
    message: str = Field(..., min_length=1)

class ServicesSendProposalArgs(BaseModel):
    """services.send_proposal args. services.send_proposal args — amount is read from the stored draft, never from args"""
    model_config = ConfigDict(extra="forbid")

    biz_proposal_id: str = Field(..., description='Resolvable fact: the stored draft is the source of party, title, amount, currency (§6.3).')
    channel: Any | None = None
