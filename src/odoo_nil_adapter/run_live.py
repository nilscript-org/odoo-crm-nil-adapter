"""Boot the shim against a LIVE Odoo backend, with credentials read from the environment ONLY.

    export ODOO_URL=https://wosool.odoo.com
    export ODOO_DB=wosool
    export ODOO_LOGIN=you@example.com      # the user the API key belongs to
    export ODOO_API_KEY=...                # NEVER commit this — keep it in .env (git-ignored)
    export NIL_BEARER=...                  # optional: front-door bearer for /nil/v0.1/*
    export NIL_EVENTS_WEBHOOK=...          # optional: control-plane ingest URL for EVENTs
    export NIL_EVENTS_SECRET=...           # optional: HMAC secret for the webhook

    uvicorn odoo_nil_adapter.run_live:build_app --factory --host 0.0.0.0 --port 8099
"""

from __future__ import annotations

import os

from odoo_nil_adapter.edge import CapturingEmitter, HttpEventEmitter, create_app
from odoo_nil_adapter.system import RealSystemClient


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name} (see .env.example)")
    return value


def build_app():
    client = RealSystemClient(
        _require("ODOO_URL"),
        db=_require("ODOO_DB"),
        login=_require("ODOO_LOGIN"),
        api_key=_require("ODOO_API_KEY"),
    )
    webhook = os.environ.get("NIL_EVENTS_WEBHOOK")
    emitter = (
        HttpEventEmitter(webhook, os.environ.get("NIL_EVENTS_SECRET", ""))
        if webhook
        else CapturingEmitter()
    )
    return create_app(client, emitter, bearer=os.environ.get("NIL_BEARER"))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=8099)
