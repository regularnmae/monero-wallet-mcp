"""Unit tests for the wallet client.

Drives `MoneroWalletRPC` with a fake `urlopen`: a success unwraps `result`, a
JSON-RPC error raises with its code, an HTTP 401 is named as an auth failure,
a transport failure is named as unreachable, and Basic auth is attached only
when credentials are configured.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error

import pytest

import rpc as rpc_module
from rpc import MoneroWalletRPC, RPCError

URL = "http://monero-wallet-rpc:18083/json_rpc"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _install(monkeypatch, *, body=None, error=None, captured=None):
    def fake_urlopen(request, timeout=None):
        if captured is not None:
            captured.append(request)
        if error is not None:
            raise error
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr(rpc_module.urllib.request, "urlopen", fake_urlopen)


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "err", email.message.Message(), io.BytesIO(body))


def test_success_returns_result(monkeypatch) -> None:
    _install(monkeypatch, body={"result": {"balance": 7}, "error": None})
    assert MoneroWalletRPC(URL).call("get_balance") == {"balance": 7}


def test_error_raises_with_code(monkeypatch) -> None:
    _install(monkeypatch, body={"result": None, "error": {"code": -21, "message": "no wallet"}})
    with pytest.raises(RPCError) as excinfo:
        MoneroWalletRPC(URL).call("get_balance")
    assert excinfo.value.code == -21
    assert excinfo.value.message == "no wallet"


def test_http_error_with_json_body_keeps_the_code(monkeypatch) -> None:
    _install(
        monkeypatch,
        error=_http_error(500, b'{"error": {"code": -1, "message": "boom"}}'),
    )
    with pytest.raises(RPCError) as excinfo:
        MoneroWalletRPC(URL).call("transfer")
    assert excinfo.value.code == -1
    assert excinfo.value.message == "boom"


def test_http_401_is_named_as_auth_failure(monkeypatch) -> None:
    _install(monkeypatch, error=_http_error(401))
    with pytest.raises(RPCError) as excinfo:
        MoneroWalletRPC(URL, "monero", "pw").call("get_balance")
    assert "authentication" in excinfo.value.message


def test_unreachable_wallet_is_named(monkeypatch) -> None:
    _install(monkeypatch, error=urllib.error.URLError("connection refused"))
    with pytest.raises(RPCError) as excinfo:
        MoneroWalletRPC(URL).call("get_balance")
    assert "cannot reach the wallet" in excinfo.value.message


def test_basic_auth_attached_only_with_credentials(monkeypatch) -> None:
    captured: list = []
    _install(monkeypatch, body={"result": 1, "error": None}, captured=captured)

    MoneroWalletRPC(URL).call("get_balance")
    assert captured[-1].get_header("Authorization") is None

    MoneroWalletRPC(URL, "monero", "pw").call("get_balance")
    assert captured[-1].get_header("Authorization") == "Basic bW9uZXJvOnB3"
