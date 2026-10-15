"""Minimal JSON-RPC client for a Monero wallet (monero-wallet-rpc).

Stdlib-only on purpose: this server POSTs JSON to one daemon on the podman
network, so it needs neither a redirect-following client nor a TLS stack, and
the smaller closure is less to audit for the server that can move funds.

Everything on monero-wallet-rpc is a JSON-RPC 2.0 method: the answer is under
`result`, a failure under `error`. The client raises one exception type,
`RPCError`, for a JSON-RPC error, an HTTP status or a transport failure, so a
tool body has a single thing to catch.
"""

import base64
import json
import urllib.error
import urllib.request
from typing import Any

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


def _error_code(error: dict[str, Any]) -> int | None:
    code = error.get("code")
    return code if isinstance(code, int) else None


class MoneroWalletRPC:
    """A client bound to one monero-wallet-rpc endpoint."""

    def __init__(
        self,
        url: str,
        user: str = "",
        password: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._url = url
        self._timeout = timeout
        # wallet-rpc is normally started with --rpc-login; attach Basic auth
        # when credentials are configured.
        if user or password:
            raw = f"{user}:{password}".encode()
            self._auth = "Basic " + base64.b64encode(raw).decode()
        else:
            self._auth = ""

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke `method`, returning `result` or raising `RPCError`."""
        body = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
        payload = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if self._auth:
            headers["Authorization"] = self._auth
        request = urllib.request.Request(
            self._url, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                parsed = _decode(response.read())
        except urllib.error.HTTPError as err:
            parsed = _decode(err.read())
            if parsed is not None and parsed.get("error"):
                raise RPCError(
                    str(parsed["error"].get("message", "rpc error")),
                    _error_code(parsed["error"]),
                ) from err
            if err.code == 401:
                raise RPCError(
                    "authentication to the wallet failed "
                    "(check MONERO_WALLET_RPC_USER / MONERO_WALLET_RPC_PASSWORD)"
                ) from err
            raise RPCError(f"HTTP {err.code} from the wallet") from err
        except urllib.error.URLError as err:
            raise RPCError(f"cannot reach the wallet at {self._url}: {err.reason}") from err

        if parsed is None:
            raise RPCError("the wallet returned a non-JSON body")
        if parsed.get("error"):
            raise RPCError(
                str(parsed["error"].get("message", "rpc error")),
                _error_code(parsed["error"]),
            )
        return parsed.get("result")


def _decode(raw: bytes) -> dict[str, Any] | None:
    """Parse a JSON object from a response body, or None if it is not one."""
    try:
        parsed = json.loads(raw.decode(errors="replace"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
