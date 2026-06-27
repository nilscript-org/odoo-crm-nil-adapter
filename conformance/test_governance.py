"""Governance policy unit tests (Phases 2 + 5): the declarative tier/sensitivity/scope table that
keeps the write ceiling honest as coverage widens to every Odoo module.

  • Tier table — generic CRUD on the writable skeleton resolves to a tier; ungranted models default-deny.
  • Sensitivity escalation — a DESTRUCTIVE op on a financial/HR model escalates to CRITICAL (owner-only).
  • Per-tenant grant — onboarding can widen the surface for one tenant without touching code or others.
  • Module scope — when an operator enables only some module groups, everything else is unexpressible.
"""

from __future__ import annotations

import pytest

from odoo_crm_nil_adapter import governance as g


@pytest.fixture(autouse=True)
def _reset() -> None:
    g.reset_policy()  # each test starts from the shipped safe defaults
    yield
    g.reset_policy()


# ── Phase 2: tier table + default-deny ────────────────────────────────────────────────────────
def test_crm_writes_carry_default_tiers() -> None:
    assert g.write_tier("crm.lead", "create") == "MEDIUM"
    assert g.write_tier("res.partner", "update") == "MEDIUM"
    assert g.write_tier("crm.lead", "delete") == "HIGH"


def test_ungranted_model_is_default_denied_for_writes() -> None:
    assert g.write_tier("account.move", "create") is None
    assert g.write_tier("hr.employee", "delete") is None
    assert g.write_tier("sale.order", "update") is None


# ── Phase 2: sensitivity classification + destructive escalation ──────────────────────────────
def test_model_class_identifies_financial_hr_system() -> None:
    assert g.model_class("account.move") == "financial"
    assert g.model_class("hr.employee") == "hr"
    assert g.model_class("res.users") == "system"
    assert g.model_class("crm.lead") == "general"


def test_destructive_op_on_financial_escalates_to_critical_once_granted() -> None:
    # A tenant deliberately grants delete on a financial model — it must NOT come back HIGH like CRM;
    # destructive financial/HR ops are owner-only CRITICAL, escalated by policy regardless of the grant.
    g.grant_write("ws_acme", "account.move", "delete", "HIGH")
    assert g.write_tier("account.move", "delete", tenant="ws_acme") == "CRITICAL"


# ── Phase 2: per-tenant grant isolation ───────────────────────────────────────────────────────
def test_per_tenant_grant_does_not_leak_to_other_tenants() -> None:
    g.grant_write("ws_acme", "account.move", "create", "HIGH")
    assert g.write_tier("account.move", "create", tenant="ws_acme") == "HIGH"
    assert g.write_tier("account.move", "create", tenant="ws_other") is None  # isolated
    assert g.write_tier("account.move", "create") is None  # global still denied


# ── Phase 2: method grants + reverse ──────────────────────────────────────────────────────────
def test_method_allowlist_default_deny_and_reverse() -> None:
    assert g.method_tier("res.partner", "message_post") == "MEDIUM"
    assert g.method_tier("account.move", "action_post") is None  # not granted by default
    g.grant_method("ws_acme", "account.move", "action_post", "HIGH", reverse="button_draft")
    assert g.method_tier("account.move", "action_post", tenant="ws_acme") == "HIGH"
    assert g.method_reverse("account.move", "action_post", tenant="ws_acme") == "button_draft"
    assert g.method_reverse("res.partner", "message_post") is None  # no reverse → IRREVERSIBLE


# ── Phase 5: module scoping ───────────────────────────────────────────────────────────────────
def test_module_scope_enabled_by_default_when_unset() -> None:
    assert g.module_enabled("crm.lead") is True
    assert g.module_enabled("account.move") is True  # no scope set → everything discoverable


def test_operator_module_scope_restricts_to_enabled_groups() -> None:
    g.set_enabled_modules({"crm"})
    assert g.module_enabled("crm.lead") is True
    assert g.module_enabled("res.partner") is True  # res.partner belongs to the crm group
    assert g.module_enabled("account.move") is False  # finance not enabled → unexpressible
    assert g.module_enabled("stock.picking") is False
