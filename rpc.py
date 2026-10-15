"""Minimal JSON-RPC client for a Monero wallet (monero-wallet-rpc).

Stdlib-only on purpose: this server POSTs JSON to one daemon on the podman
network, so it needs neither a redirect-following client nor a TLS stack, and
the smaller closure is less to audit for the server that can move funds.

Authentication is **HTTP digest** (RFC 2617, MD5), and it has two quirks that
this client exists to handle:

- monero-wallet-rpc, started with `--rpc-login`, advertises a `Digest`
  challenge and rejects a plain Basic header with 401;
- the challenge nonce is bound to the **TCP connection**, so the authenticated
  retry must travel on the same socket the challenge arrived on. A fresh
  connection makes the server answer `stale=true` and 401 again -- which is why
  `urllib` (a new connection per request) cannot be used here, even with its
  own digest handler.

So `call()` opens one connection per call, sends the request, and on a 401
answers the challenge on that same connection, then closes it. One connection
per call (rather than a shared, locked one) keeps this thread-safe with no
state, which anyio's worker threads need.

The client raises one exception type, `RPCError`, for a JSON-RPC error, an HTTP
status or a transport failure, so a tool body has a single thing to catch.
"""

import hashlib
import http.client
import json
import os
from typing import Any
from urllib.parse import urlsplit

DEFAULT_TIMEOUT = 60.0


class RPCError(Exception):
    """A failed RPC call.

    `message` is the wallet's human-readable text; `code` is its numeric error
    code when supplied, or None for a transport failure or plain HTTP status.
    """

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _split_challenge(value: str) -> list[str]:
    """Split a comma-separated auth header without breaking quoted values
    (monero's nonce is base64 and can contain '=' and '+')."""
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _parse_challenge(header: str) -> dict[str, str]:
    """Parse a `WWW-Authenticate: Digest ...` header into its parameters."""
    _, _, rest = header.partition(" ")
    params: dict[str, str] = {}
    for part in _split_challenge(rest):
        key, _, value = part.partition("=")
        params[key.strip().lower()] = value.strip().strip('"')
    return params


def _error_code(error: dict[str, Any]) -> int | None:
    code = error.get("code")
    return code if isinstance(code, int) else None


def digest_authorization(
    user: str,
    password: str,
    challenge: str,
    method: str,
    uri: str,
    cnonce: str | None = None,
    nc: str = "00000001",
) -> str:
    """Build the `Authorization: Digest ...` header for a challenge (RFC 2617).

    `cnonce`/`nc` are parameters so a test can pin the RFC 2617 example vector;
    in production a random `cnonce` is generated.
    """
    params = _parse_challenge(challenge)
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    qop = params.get("qop", "")
    if cnonce is None:
        cnonce = hashlib.sha1(os.urandom(16)).hexdigest()[:16]

    ha1 = _md5(f"{user}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop:
        response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")

    fields = [
        f'username="{user}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        "algorithm=MD5",
    ]
    if qop:
        fields += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
    return "Digest " + ", ".join(fields)


class MoneroWalletRPC:
    """A client bound to one monero-wallet-rpc endpoint."""

    def __init__(
        self,
        url: str,
        user: str = "",
        password: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        parts = urlsplit(url)
        self._url = url
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._path = parts.path or "/"
        self._secure = parts.scheme == "https"
        self._user = user
        self._password = password
        self._timeout = timeout

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke `method`, returning `result` or raising `RPCError`."""
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
        ).encode()

        connection = self._connect()
        try:
            status, raw, challenge = self._post(connection, payload, "")
            if status == 401 and (self._user or self._password):
                # The nonce is bound to this connection; answer it here.
                authorization = digest_authorization(
                    self._user, self._password, challenge, "POST", self._path
                )
                status, raw, challenge = self._post(connection, payload, authorization)
        except (OSError, http.client.HTTPException) as err:
            raise RPCError(f"cannot reach the wallet at {self._url}: {err}") from err
        finally:
            connection.close()

        parsed = _decode(raw)
        if parsed is None:
            if status == 401:
                raise RPCError("authentication to the wallet failed")
            raise RPCError(f"HTTP {status} from the wallet")
        if parsed.get("error"):
            raise RPCError(
                str(parsed["error"].get("message", "rpc error")),
                _error_code(parsed["error"]),
            )
        return parsed.get("result")

    def _connect(self) -> http.client.HTTPConnection:
        if self._secure:
            return http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout)
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def _post(
        self, connection: http.client.HTTPConnection, payload: bytes, authorization: str
    ) -> tuple[int, bytes, str]:
        """POST on `connection`, returning (status, body, WWW-Authenticate).

        A 401 is returned, not raised, so `call` can answer the challenge.
        """
        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        connection.request("POST", self._path, body=payload, headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, body, response.getheader("WWW-Authenticate", "")


def _decode(raw: bytes) -> dict[str, Any] | None:
    """Parse a JSON object from a response body, or None if it is not one."""
    try:
        parsed = json.loads(raw.decode(errors="replace"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
