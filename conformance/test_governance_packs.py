# conformance/test_governance_packs.py
from odoo_nil_adapter import governance, packs


def test_write_tier_crm_lead():
    governance.reset_policy()
    assert governance.write_tier("crm.lead", "create") == "MEDIUM"


def test_write_tier_account_move_is_none_without_grant():
    governance.reset_policy()
    # account.move has no write_targets in FINANCE pack, so no default write tier
    assert governance.write_tier("account.move", "create") is None


def test_method_tier_from_packs():
    governance.reset_policy()
    assert governance.method_tier("res.partner", "message_post") == "MEDIUM"


def test_reads_allowed_uses_declared_targets():
    governance.reset_policy()
    assert governance.reads_allowed_raw("crm.lead") is True


def test_writable_targets_from_packs():
    governance.reset_policy()
    targets = governance.writable_targets()
    assert "crm.lead" in targets
    assert "res.partner" in targets
