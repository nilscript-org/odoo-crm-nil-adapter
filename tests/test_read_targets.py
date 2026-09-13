"""D37 (Task 1.1): the Odoo read plane understands business names on every read verb, not just
`nil.intent`. Before this, `_run_nil_search` and its siblings handed `args["target"]` straight to the
plane — a business name like `Customer` has no Odoo table, so it refused `CAPABILITY_UNSUPPORTED` even
though `nil.intent` already resolved the identical name correctly via `_OdooBindings.resolve_target`.
"""

from __future__ import annotations

from odoo_nil_adapter import translate


def test_search_resolves_the_business_name_before_the_plane(fake_client) -> None:
    translate._run_nil_search(fake_client, {"target": "Customer", "filter": [], "limit": 1})
    assert fake_client.plane.last_target == "res.partner"


def test_an_unknown_name_still_refuses_in_the_engine(fake_client) -> None:
    out = translate._run_nil_search(fake_client, {"target": "Unicorn", "filter": []})
    assert out["outcome"] == "refused" and out["code"] == "CAPABILITY_UNSUPPORTED"
