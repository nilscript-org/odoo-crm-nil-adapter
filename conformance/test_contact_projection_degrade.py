"""find_contact/get_contact must DEGRADE their projection, not go silently empty, when the Odoo
build lacks optional channel fields (Odoo 18 removed res.partner.mobile — proven live:
'ValueError: Invalid field mobile on res.partner' faulted the whole search_read)."""

from __future__ import annotations

from typing import Any

from odoo_nil_adapter.system import SystemError
from odoo_nil_adapter.translate import QUERY_VERBS


class Odoo18Like:
    """search() faults whenever the projection names a field this build lacks."""

    MISSING = {"mobile", "phone_sanitized"}
    ROW = {"id": 9, "name": "ALI", "email": False, "phone": False}

    def search(self, target, domain, *, fields=None, limit=50):
        if fields and set(fields) & self.MISSING:
            bad = sorted(set(fields) & self.MISSING)[0]
            raise SystemError(f"odoo {target}.search_read: ValueError: Invalid field '{bad}' on '{target}'")
        return [dict(self.ROW)]


def test_find_contact_degrades_to_base_projection():
    out = QUERY_VERBS["crm.find_contact"].run(Odoo18Like(), {"query": "ALI"})
    assert out["count"] >= 1 and out["items"][0]["name"] == "ALI", out


def test_get_contact_degrades_to_base_projection():
    out = QUERY_VERBS["crm.get_contact"].run(Odoo18Like(), {"contact_id": "9"})
    assert out["found"] is True and out["item"]["name"] == "ALI", out


def test_blank_query_lists_recent_with_degrade():
    out = QUERY_VERBS["crm.find_contact"].run(Odoo18Like(), {"query": ""})
    assert out["count"] >= 1, out
