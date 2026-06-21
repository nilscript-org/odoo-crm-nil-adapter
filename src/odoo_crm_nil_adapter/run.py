"""Boot the shim against the in-memory FakeSystem (a smoke run, no live backend)."""

from __future__ import annotations

from odoo_crm_nil_adapter.edge import CapturingEmitter, create_app
from odoo_crm_nil_adapter.system import FakeSystem


def build_demo_app():
    return create_app(FakeSystem(), CapturingEmitter(), bearer=None)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(build_demo_app(), host="127.0.0.1", port=8099)
