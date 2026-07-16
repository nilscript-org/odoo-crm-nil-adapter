"""G24 — the shared XML-RPC proxy must never be used by two calls at once.

Witnessed live 2026-07-16 20:57 (ws_acme, bulk fan-out): two concurrent governed commits through
this adapter collided on the ONE `xmlrpc.client.ServerProxy` the cached RealSystemClient holds —
http.client is a state machine (Idle → Request-sent → …), so interleaved calls raised
"CannotSendRequest: Request-sent" / "ResponseNotReady: Idle" and BOTH writes ended
failed_terminal. The edge runs sync endpoints on a threadpool, so any concurrent pair of requests
for the same tenant shares the proxy.

The client, not the caller, owns this invariant: every proxy invocation (authenticate, version
probe, execute_kw) is serialized under one lock. The lock wraps ONLY the wire call — never the
retry backoff sleeps — so a rate-limited retry does not freeze the whole tenant.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from odoo_nil_adapter.system import RealSystemClient


class _OverlapDetectingProxy:
    """A fake xmlrpc endpoint that records whether two calls ever overlap in time."""

    def __init__(self) -> None:
        self._busy = False
        self._guard = threading.Lock()
        self.collisions = 0
        self.calls = 0

    def _enter(self) -> None:
        with self._guard:
            if self._busy:
                self.collisions += 1
            self._busy = True
            self.calls += 1

    def _exit(self) -> None:
        with self._guard:
            self._busy = False

    # the two surfaces RealSystemClient invokes
    def authenticate(self, db: str, login: str, key: str, ctx: dict) -> int:
        self._enter()
        time.sleep(0.005)
        self._exit()
        return 7

    def version(self) -> dict[str, Any]:
        self._enter()
        time.sleep(0.005)
        self._exit()
        return {"server_version_info": [19, 3, 0]}

    def execute_kw(self, db: str, uid: int, key: str, model: str, method: str,
                   args: list, kw: dict) -> Any:
        self._enter()
        time.sleep(0.005)  # long enough that unserialized threads WILL overlap
        self._exit()
        return [{"id": 1, "name": "x"}]


def test_concurrent_governed_calls_never_share_the_wire() -> None:
    proxy = _OverlapDetectingProxy()
    client = RealSystemClient("http://x", db="d", login="l", api_key="k", sleep=lambda _s: None)
    client._common = proxy  # noqa: SLF001 — the injection seam the durability tests use
    client._models = proxy  # noqa: SLF001

    errors: list[BaseException] = []

    def hammer() -> None:
        try:
            for _ in range(10):
                client.get("res.partner", "1")
        except BaseException as exc:  # noqa: BLE001 — a state-machine crash IS the defect
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors[:2]
    assert proxy.calls >= 80
    assert proxy.collisions == 0, (
        f"{proxy.collisions} overlapping proxy calls — the live 'Request-sent'/'Idle' collision"
    )
