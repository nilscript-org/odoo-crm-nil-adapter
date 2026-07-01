# conformance/test_packs.py
from odoo_crm_nil_adapter import packs, translate


def test_registry_exposes_crm_pack():
    names = {p.name for p in packs.PACKS}
    assert "crm" in names


def test_translate_write_verbs_come_from_packs():
    # crm.create_lead is contributed by the crm pack and surfaces in the aggregate
    assert "crm.create_lead" in translate.WRITE_VERBS
    assert translate.WRITE_VERBS["crm.create_lead"].doctype == "crm.lead"


def test_declared_targets_is_union_of_pack_write_targets():
    assert "crm.lead" in translate.DECLARED_TARGETS
    assert "res.partner" in translate.DECLARED_TARGETS


def test_finance_and_inventory_packs_present():
    names = {p.name for p in packs.PACKS}
    assert {"finance", "sales", "inventory"} <= names


def test_governance_module_models_from_packs():
    from odoo_crm_nil_adapter import governance
    assert governance.model_class("account.move") == "financial"
    # inventory prefixes come from the pack
    assert governance.module_enabled("stock.picking") is True


def test_read_projection_from_pack():
    from odoo_crm_nil_adapter import read_plane
    assert read_plane._TARGET_FIELDS["account.move"][0] == "id"
    assert "amount_total" in read_plane._TARGET_FIELDS["account.move"]
