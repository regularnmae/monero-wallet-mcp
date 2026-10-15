"""Unit tests for the wallet client.

The digest maths is pinned to the RFC 2617 example vector, and `call` is driven
with fake `_connect`/`_post` seams so the challenge/retry flow (on one
connection) and the error paths are covered without a wallet.
"""

from __future__ import annotations

import json

import pytest

from rpc import MoneroWalletRPC, RPCError, digest_authorization

URL = "http://monero-wallet-rpc:18083/json_rpc"

# RFC 2617 section 3.5 worked example.
RFC_CHALLENGE = (
    'Digest realm="testrealm@host.com", qop="auth", '
    'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
    'opaque="5ccc069c403ebaf9f0171e9517f40e41"'
)
MONERO_CHALLENGE = (
    'Digest qop="auth",algorithm=MD5,realm="monero-rpc",'
    'nonce="0sR8LER1Iyyaf6rnJe59ZQ==",stale=false'
)


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_digest_matches_the_rfc_2617_vector() -> None:
    header = digest_authorization(
        "Mufasa",
        "Circle Of Life",
        RFC_CHALLENGE,
        "GET",
        "/dir/index.html",
        cnonce="0a4f113b",
    )
    assert 'response="6629fae49393a05397450978507c4ef1"' in header


def test_challenge_parser_keeps_a_base64_nonce_intact() -> None:
    header = digest_authorization("monero", "pw", MONERO_CHALLENGE, "POST", "/json_rpc")
    assert 'nonce="0sR8LER1Iyyaf6rnJe59ZQ=="' in header
    assert header.startswith("Digest ")


def _install(client, monkeypatch, post) -> _FakeConn:
    conn = _FakeConn()
    monkeypatch.setattr(client, "_connect", lambda: conn)
    monkeypatch.setattr(client, "_post", post)
    return conn


def test_call_answers_a_401_and_retries_with_digest(monkeypatch) -> None:
    client = MoneroWalletRPC(URL, "monero", "pw")
    seen: list[str] = []

    def post(connection, payload, authorization):
        seen.append(authorization)
        if len(seen) == 1:
            return 401, b"", MONERO_CHALLENGE
        return 200, json.dumps({"result": {"balance": 7}, "error": None}).encode(), ""

    conn = _install(client, monkeypatch, post)
    assert client.call("get_balance") == {"balance": 7}
    assert seen[0] == ""  # first attempt unauthenticated
    assert seen[1].startswith("Digest ")  # retried with digest
    assert conn.closed  # the connection is always closed


def test_call_without_credentials_does_not_retry(monkeypatch) -> None:
    client = MoneroWalletRPC(URL)  # no user/password
    calls = []

    def post(connection, payload, authorization):
        calls.append(authorization)
        return 401, b"", MONERO_CHALLENGE

    _install(client, monkeypatch, post)
    with pytest.raises(RPCError) as excinfo:
        client.call("get_balance")
    assert "authentication" in excinfo.value.message
    assert calls == [""]


def test_error_object_raises_with_code(monkeypatch) -> None:
    client = MoneroWalletRPC(URL, "monero", "pw")
    _install(
        client,
        monkeypatch,
        lambda connection, payload, authorization: (
            200,
            json.dumps({"result": None, "error": {"code": -21, "message": "no wallet"}}).encode(),
            "",
        ),
    )
    with pytest.raises(RPCError) as excinfo:
        client.call("get_balance")
    assert excinfo.value.code == -21
    assert excinfo.value.message == "no wallet"


def test_non_json_body_reports_the_http_status(monkeypatch) -> None:
    client = MoneroWalletRPC(URL, "monero", "pw")
    _install(
        client,
        monkeypatch,
        lambda connection, payload, authorization: (500, b"<html>boom</html>", ""),
    )
    with pytest.raises(RPCError) as excinfo:
        client.call("get_balance")
    assert "HTTP 500" in excinfo.value.message


def test_transport_error_is_named(monkeypatch) -> None:
    client = MoneroWalletRPC(URL, "monero", "pw")

    def boom(connection, payload, authorization):
        raise OSError("connection refused")

    _install(client, monkeypatch, boom)
    with pytest.raises(RPCError) as excinfo:
        client.call("get_balance")
    assert "cannot reach the wallet" in excinfo.value.message
