"""Durability (adapter slice): the RealSystemClient retries transient transport faults with backoff —
the "429 flood" lesson — while an application Fault stays terminal. Orchestration-level durability
(crash-resume, per-tenant queues) is the separate Temporal plan; this proves a SINGLE call is resilient.
"""

from __future__ import annotations

import xmlrpc.client

import pytest

from odoo_nil_adapter.system import RealSystemClient, SystemError


class _FlakyProxy:
    """Stand-in for the Odoo XML-RPC object endpoint: fail the first `fail_times` calls with `exc`,
    then return `ok`. Records how many times it was called so the test can assert the retry count."""

    def __init__(self, exc: Exception, fail_times: int, ok: object = 42) -> None:
        self._exc, self._fail_times, self._ok, self.calls = exc, fail_times, ok, 0

    def execute_kw(self, *_args, **_kw) -> object:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return self._ok


def _client(proxy: _FlakyProxy, **kw) -> RealSystemClient:
    c = RealSystemClient("http://x", db="d", login="l", api_key="k", sleep=lambda _s: None, **kw)
    c._uid = 1  # skip auth
    c._models = proxy
    return c


def test_retries_rate_limit_then_succeeds() -> None:
    proxy = _FlakyProxy(xmlrpc.client.ProtocolError("u", 429, "Too Many Requests", {}), fail_times=2)
    client = _client(proxy, max_retries=3, backoff=0.0)
    assert client._kw("crm.lead", "search_count", [[]]) == 42
    assert proxy.calls == 3  # two 429s retried, third succeeded


def test_gives_up_after_max_retries_on_persistent_rate_limit() -> None:
    proxy = _FlakyProxy(xmlrpc.client.ProtocolError("u", 503, "Service Unavailable", {}), fail_times=99)
    client = _client(proxy, max_retries=2, backoff=0.0)
    with pytest.raises(SystemError):
        client._kw("crm.lead", "search_count", [[]])
    assert proxy.calls == 3  # initial + 2 retries, then surfaced


def test_application_fault_is_terminal_not_retried() -> None:
    proxy = _FlakyProxy(xmlrpc.client.Fault(2, "AccessError: not allowed"), fail_times=99)
    client = _client(proxy, max_retries=3, backoff=0.0)
    with pytest.raises(SystemError):
        client._kw("account.move", "create", [{}])
    assert proxy.calls == 1  # a Fault is a real answer — never retried


def test_throttle_holds_minimum_interval() -> None:
    proxy = _FlakyProxy(xmlrpc.client.Fault(1, "x"), fail_times=0, ok=7)
    slept: list[float] = []
    clock = {"t": 0.0}
    client = RealSystemClient("http://x", db="d", login="l", api_key="k",
                              min_interval=1.0, sleep=lambda s: slept.append(s),
                              monotonic=lambda: clock["t"])
    client._uid = 1
    client._models = proxy
    client._kw("crm.lead", "search_count", [[]])  # first call: no prior, no wait
    client._kw("crm.lead", "search_count", [[]])  # immediate second: must wait ~1.0
    assert slept and slept[-1] == pytest.approx(1.0)
