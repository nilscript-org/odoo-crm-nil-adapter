"""U5 round 2: `HttpEventEmitter` signs events with this adapter's own service key
(`X-NIL-Key-Id` + `cp_auth.event_mac`) when one is configured, the shared-secret HMAC otherwise —
byte for byte unchanged from today when no key is set.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from odoo_nil_adapter import cp_auth
from odoo_nil_adapter.edge import HttpEventEmitter

KEY_ID = "kid-example"
KEY_SECRET = "0123456789abcdef0123456789abcdef"
SHARED_SECRET = "shared-secret-today"
ENVELOPE = {"type": "test.vector", "n": 1}


def _capture_post(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_post(url: str, *, content: bytes, headers: dict[str, str], timeout: float) -> None:
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers

    monkeypatch.setattr("httpx.post", fake_post)
    return captured


def test_emit_without_key_uses_todays_shared_signature_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_post(monkeypatch)
    emitter = HttpEventEmitter("https://cp.example/events", SHARED_SECRET, source="mcp")

    emitter.emit(ENVELOPE, sequence=7)

    raw = json.dumps(ENVELOPE, separators=(",", ":")).encode("utf-8")
    expected_signature = hmac.new(SHARED_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    assert captured["content"] == raw
    assert captured["headers"]["X-NIL-Signature"] == expected_signature
    assert captured["headers"]["X-NIL-Sequence"] == "7"
    assert captured["headers"]["X-NIL-Source"] == "mcp"
    assert "X-NIL-Key-Id" not in captured["headers"]


def test_emit_with_key_sends_key_id_and_event_mac_not_shared_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_post(monkeypatch)
    emitter = HttpEventEmitter(
        "https://cp.example/events",
        SHARED_SECRET,
        own_key=(KEY_ID, KEY_SECRET),
    )

    emitter.emit(ENVELOPE, sequence=3)

    raw = json.dumps(ENVELOPE, separators=(",", ":")).encode("utf-8")
    expected_mac = cp_auth.event_mac(KEY_SECRET, raw)
    shared_signature = hmac.new(SHARED_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    assert captured["headers"]["X-NIL-Key-Id"] == KEY_ID
    assert captured["headers"]["X-NIL-Signature"] == expected_mac
    assert captured["headers"]["X-NIL-Signature"] != shared_signature
    assert captured["headers"]["X-NIL-Sequence"] == "3"


def test_emit_with_key_signs_the_exact_posted_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_post(monkeypatch)
    emitter = HttpEventEmitter(
        "https://cp.example/events", SHARED_SECRET, own_key=(KEY_ID, KEY_SECRET)
    )

    emitter.emit(ENVELOPE, sequence=1)

    assert captured["headers"]["X-NIL-Signature"] == cp_auth.event_mac(
        KEY_SECRET, captured["content"]
    )
