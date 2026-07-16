"""THE PORTAL IS THE THIRD HONEST ROUTE TO THE ERP'S OWN DOCUMENT.

Witnessed live 2026-07-16 on ahmedco2.odoo.com (saas~19.3): the two routes `render_report` shipped
with are BOTH closed on modern Odoo SaaS —

  • XML-RPC `render_qweb_pdf` does not exist (renamed `_render_qweb_pdf` in 15+, and `execute_kw`
    refuses any method starting with `_`), and
  • `/web/session/authenticate` answers **AccessDenied** to an API key: RPC-scoped keys open no web
    session, so the `/report/pdf/...` controller is unreachable with the credentials this adapter
    holds.

What IS open — and rendered a real 29 KB `%PDF` for PO/1 on the live tenant — is Odoo's own portal
document controller: `GET /my/purchase/<id>?report_type=pdf&download=true&access_token=<token>`.
It is token-gated (no session), and it renders the SAME QWeb report Odoo mails the vendor. The
token is the record's own `access_token` (portal.mixin); when absent, Odoo itself mints a uuid4
lazily via `_portal_ensure_token` — the portal route here does exactly that and nothing more.

These tests pin the fallback chain on a saas-19-shaped backend: session refused → portal renders.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from odoo_nil_adapter import system as system_mod
from odoo_nil_adapter.system import RealSystemClient, SystemError

PDF = b"%PDF-1.7\nthe portal-rendered purchase order\n%%EOF\n"


class _FakeCommon:
    def __init__(self, major: int = 19) -> None:
        self._major = major

    def version(self) -> dict[str, Any]:
        return {"server_version": f"saas~{self._major}.3+e",
                "server_version_info": [self._major, 3, 0, "final", 0, "e"]}

    def authenticate(self, db: str, login: str, key: str, ctx: dict) -> int:
        return 2


class _FakeModels:
    """The XML-RPC object endpoint: serves the access_token read/write and refuses everything the
    live saas~19 refused (no public render_qweb_pdf)."""

    def __init__(self, access_token: str | bool = False) -> None:
        self.access_token = access_token
        self.writes: list[tuple[str, list, dict]] = []

    def execute_kw(self, db: str, uid: int, key: str, model: str, method: str,
                   args: list, kw: dict) -> Any:
        if model == "ir.actions.report" and method == "search_read":
            return [{"id": 77}]
        if model == "ir.actions.report" and method == "render_qweb_pdf":
            raise system_mod.xmlrpc.client.Fault(
                2, "AttributeError: The method 'ir.actions.report.render_qweb_pdf' does not exist")
        if method == "read":
            return [{"id": args[0][0], "access_token": self.access_token}]
        if method == "write":
            self.writes.append((model, args[0], args[1]))
            self.access_token = args[1]["access_token"]
            return True
        raise AssertionError(f"unexpected xmlrpc call {model}.{method}")


class _FakeResponse:
    def __init__(self, data: bytes, content_type: str) -> None:
        self._data = data
        self.headers = {"Content-Type": content_type}
        self.status = 200

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeOpener:
    """Answers like the live saas~19 tenant: session auth is AccessDenied for an API key; the
    portal controller serves the PDF when (and only when) the record's token is presented."""

    def __init__(self, models: _FakeModels, portal_body: bytes = PDF,
                 portal_type: str = "application/pdf") -> None:
        self._models = models
        self._portal_body = portal_body
        self._portal_type = portal_type
        self.urls: list[str] = []

    def open(self, req: Any, timeout: float = 0) -> _FakeResponse:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.urls.append(url)
        if url.endswith("/web/session/authenticate"):
            return _FakeResponse(json.dumps(
                {"jsonrpc": "2.0", "error": {"data": {"name": "odoo.exceptions.AccessDenied"}}}
            ).encode(), "application/json")
        if "/my/purchase/" in url or "/my/invoices/" in url:
            token = self._models.access_token
            if token and f"access_token={token}" in url:
                return _FakeResponse(self._portal_body, self._portal_type)
            return _FakeResponse(b"<html>403 not allowed</html>", "text/html")
        raise AssertionError(f"unexpected HTTP call: {url}")


@pytest.fixture()
def saas19(monkeypatch: pytest.MonkeyPatch):
    def _build(access_token: str | bool = False, portal_body: bytes = PDF,
               portal_type: str = "application/pdf"):
        client = RealSystemClient("https://ahmedco2.example", db="d", login="l", api_key="k",
                                  sleep=lambda _s: None)
        models = _FakeModels(access_token)
        opener = _FakeOpener(models, portal_body, portal_type)
        client._common = _FakeCommon(19)  # noqa: SLF001 — the injection seam these tests exist for
        client._models = models  # noqa: SLF001
        monkeypatch.setattr(system_mod, "build_opener", lambda *_h: opener)
        return client, models, opener

    return _build


class TestSaas19FallsBackToThePortal:
    def test_session_refused_then_the_portal_renders_the_erps_pdf(self, saas19) -> None:
        client, models, opener = saas19()

        pdf = client.render_report("purchase.report_purchaseorder", "purchase.order", "1")

        assert pdf == PDF  # the portal's bytes ARE the document
        assert any("/my/purchase/1?" in u for u in opener.urls)

    def test_a_missing_token_is_minted_once_exactly_like_portal_ensure_token(self, saas19) -> None:
        client, models, opener = saas19(access_token=False)

        client.render_report("purchase.report_purchaseorder", "purchase.order", "1")

        assert len(models.writes) == 1
        model, ids, vals = models.writes[0]
        assert (model, ids) == ("purchase.order", [1])
        uuid.UUID(vals["access_token"])  # a uuid4 string — the exact shape Odoo itself mints

    def test_an_existing_token_is_reused_and_nothing_is_written(self, saas19) -> None:
        tok = "11111111-2222-3333-4444-555555555555"
        client, models, opener = saas19(access_token=tok)

        client.render_report("purchase.report_purchaseorder", "purchase.order", "1")

        assert models.writes == []  # the READ verb wrote nothing
        assert any(f"access_token={tok}" in u for u in opener.urls)

    def test_the_invoice_document_routes_to_my_invoices(self, saas19) -> None:
        client, models, opener = saas19()

        # the invoice portal path serves account.move
        _FakeModelsRead = models.execute_kw  # same fake serves account.move reads
        pdf = client.render_report("account.report_invoice", "account.move", "9")

        assert pdf == PDF
        assert any("/my/invoices/9?" in u for u in opener.urls)


class TestPortalFailureIsARefusalNeverAFabrication:
    def test_html_from_the_portal_is_not_a_document(self, saas19) -> None:
        client, models, opener = saas19(portal_body=b"<html>error</html>", portal_type="text/html")

        with pytest.raises(SystemError) as exc:
            client.render_report("purchase.report_purchaseorder", "purchase.order", "1")

        assert "not a PDF" in str(exc.value)

    def test_a_target_with_no_portal_page_refuses_instead_of_guessing_a_url(self, saas19) -> None:
        client, models, opener = saas19()

        with pytest.raises(SystemError) as exc:
            client.render_report("some.report", "res.partner", "3")

        assert "portal" in str(exc.value).lower()
        assert not any("/my/" in u for u in opener.urls)  # no URL was invented for it
