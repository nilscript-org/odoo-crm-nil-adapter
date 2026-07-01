# conformance/test_purchasing.py
from odoo_crm_nil_adapter import packs, translate, governance


def test_purchase_create_order_shapes_x2many_order_line():
    v = translate.WRITE_VERBS["purchase.create_order"]
    doc = v.to_native({
        "partner_id": 42, "date_order": "2026-06-25 00:00:00", "origin": "SEWAR-REPLEN-0001",
        "lines": [{"product_id": 1001, "product_qty": 100, "product_uom": 1,
                   "price_unit": 3.5, "taxes_id": [5]}],
    })
    assert doc["partner_id"] == 42
    assert doc["origin"] == "SEWAR-REPLEN-0001"
    # order_line is Odoo create-child command tuples
    assert doc["order_line"][0][0] == 0 and doc["order_line"][0][1] == 0
    line = doc["order_line"][0][2]
    assert line["product_id"] == 1001 and line["product_qty"] == 100.0
    assert line["price_unit"] == 3.5
    # taxes_id is the (6,0,[ids]) replace command
    assert line["taxes_id"] == [(6, 0, [5])]


def test_purchase_confirm_is_compensable():
    v = translate.WRITE_VERBS["purchase.confirm_order"]
    assert v.op == "method" and v.method == "button_confirm"
    assert v.reverse_method == "button_cancel"


def test_purchase_order_writable_and_confirm_granted_by_default():
    governance.reset_policy()  # all modules enabled
    assert governance.write_tier("purchase.order", "create") == "MEDIUM"
    assert governance.method_tier("purchase.order", "button_confirm") == "HIGH"
    assert governance.method_reverse("purchase.order", "button_confirm") == "button_cancel"


def test_purchasing_unexpressible_when_module_disabled():
    governance.set_enabled_modules({"crm"})  # purchasing OUT of scope
    assert governance.write_tier("purchase.order", "create") is None
    assert governance.method_tier("purchase.order", "button_confirm") is None
    governance.reset_policy()


def test_purchasing_ungranted_model_still_denied():
    # a purchase.* model NOT declared by the pack is unexpressible even with purchasing enabled
    governance.reset_policy()
    assert governance.write_tier("purchase.requisition", "create") is None
