import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from rpc import MoneroWalletRPC, RPCError

logger = logging.getLogger("monero-wallet-mcp")

_LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVELS: tuple[_LogLevel, ...] = get_args(_LogLevel)

# Atomic units (piconero) per XMR. Amounts are accepted and reported in XMR,
# but the wallet speaks atomic units, so every amount is converted here and
# nowhere else.
ATOMIC_PER_XMR = 1_000_000_000_000

_PRIORITY = {"unimportant": 1, "normal": 2, "elevated": 3, "priority": 4}


def _log_level() -> _LogLevel:
    requested = os.environ.get("LOG_LEVEL", "INFO").upper()
    return requested if requested in _LEVELS else "INFO"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


def _split_set(name: str) -> frozenset[str]:
    return frozenset(part.strip() for part in os.environ.get(name, "").split(",") if part.strip())


# --- config ---------------------------------------------------------------
MONERO_WALLET_RPC_URL = os.environ.get(
    "MONERO_WALLET_RPC_URL", "http://monero-wallet-rpc:18083/json_rpc"
)
MONERO_WALLET_RPC_USER = os.environ.get("MONERO_WALLET_RPC_USER", "")
MONERO_WALLET_RPC_PASSWORD = os.environ.get("MONERO_WALLET_RPC_PASSWORD", "")
RPC_TIMEOUT = _env_float("MONERO_WALLET_RPC_TIMEOUT", 60.0)

# Spending is OFF by default. With it off, the spend tools are not registered
# at all, so a read-only deployment cannot move funds even by mistake.
ENABLE_SPENDING = os.environ.get("ENABLE_SPENDING", "") == "1"

# Caps are in XMR. Enabling spending requires both to be set and positive:
# "unlimited" is not a default worth having on a mainnet wallet.
MAX_SPEND_PER_CALL = _env_float("MAX_SPEND_PER_CALL", 0.0)
MAX_SPEND_PER_DAY = _env_float("MAX_SPEND_PER_DAY", 0.0)

# Optional address allowlist; empty means any address is allowed.
ALLOWED_DESTINATIONS = _split_set("ALLOWED_DESTINATIONS")

if ENABLE_SPENDING and (MAX_SPEND_PER_CALL <= 0 or MAX_SPEND_PER_DAY <= 0):
    raise RuntimeError(
        "ENABLE_SPENDING=1 requires MAX_SPEND_PER_CALL and MAX_SPEND_PER_DAY "
        "to be set to positive values (in XMR)."
    )

MCP_ALLOW_ANONYMOUS = os.environ.get("MCP_ALLOW_ANONYMOUS", "") == "1"
HEALTH_PATH = "/health"

# The read token gates the whole endpoint; the spend token is additionally
# required for the write tools. With spending enabled a spend token must be
# set, so the read token alone can never move funds.
TOKEN_ENV = "MONERO_MCP_TOKEN"
SPEND_TOKEN_ENV = "MONERO_MCP_SPEND_TOKEN"

rpc = MoneroWalletRPC(
    MONERO_WALLET_RPC_URL, MONERO_WALLET_RPC_USER, MONERO_WALLET_RPC_PASSWORD, timeout=RPC_TIMEOUT
)

# Every tool name that changes wallet state. The middleware refuses these to
# the read token.
SPEND_TOOLS = frozenset({"transfer", "sweep_all", "create_address"})


mcp = MCPServer(
    "Monero Wallet",
    instructions=(
        "Access to a Monero wallet (monero-wallet-rpc). Read tools report "
        "balance, addresses and transfers. Spending tools (transfer, "
        "sweep_all, create_address) are present only when the operator "
        "enabled them, take an explicit `dry_run` flag (default true), and "
        "are subject to per-call and per-day caps. Prefer dry_run=true to "
        "preview a transfer before committing it."
    ),
    log_level=_log_level(),
)

_READ_TOOLS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_WRITE_TOOL = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


class SpendRefused(Exception):
    """A spend was rejected locally by a cap or the allowlist."""


class DailyBudget:
    """Tracks committed spend against a per-day ceiling, in XMR.

    Only non-dry-run spends are reserved. A day is UTC; the counter resets
    when the date rolls over. Guarded by a lock because tools run on worker
    threads.
    """

    def __init__(self, limit: float, clock: Callable[[], float] = time.time) -> None:
        self._limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._day = ""
        self._spent = 0.0

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=UTC).strftime("%Y-%m-%d")

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._spent = 0.0

    def reserve(self, amount: float) -> None:
        with self._lock:
            self._roll()
            if self._spent + amount > self._limit:
                raise SpendRefused(
                    f"daily cap of {self._limit} XMR would be exceeded "
                    f"({self._spent} XMR already committed today)"
                )
            self._spent += amount

    def release(self, amount: float) -> None:
        with self._lock:
            self._roll()
            self._spent = max(0.0, self._spent - amount)


budget = DailyBudget(MAX_SPEND_PER_DAY)


def _render(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


def _error_text(err: RPCError) -> str:
    if err.code is None:
        return err.message
    return f"{err.message} (RPC error {err.code})"


def _call(method: str, params: dict[str, Any] | None = None) -> str:
    """Run one read RPC and render the result, or a readable error."""
    try:
        return _render(rpc.call(method, params))
    except RPCError as err:
        logger.warning("%s failed: %s", method, err.message)
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in %s", method)
        return f"Unexpected error calling {method}: {err}"


def _audit(action: str, amount: float, destination: str, detail: str) -> None:
    """One structured line per spend attempt."""
    logger.info(
        "SPEND action=%s amount=%s XMR destination=%s detail=%s",
        action,
        amount,
        destination or "-",
        detail,
    )


def _precheck(amount: float, destination: str) -> str | None:
    """Return a refusal message, or None when the spend may proceed."""
    if amount <= 0:
        return "amount must be greater than zero"
    if amount > MAX_SPEND_PER_CALL:
        return f"amount {amount} XMR exceeds the per-call cap of {MAX_SPEND_PER_CALL} XMR"
    if ALLOWED_DESTINATIONS and destination not in ALLOWED_DESTINATIONS:
        return "destination is not in the configured ALLOWED_DESTINATIONS allowlist"
    return None


# --- read tools -----------------------------------------------------------


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def get_balance(account_index: Annotated[int, Field(ge=0)] = 0) -> str:
    """Return the wallet's balance.

    Includes total, unlocked (spendable) and locked balances, in atomic units
    and as XMR.

    Args:
        account_index: Account to query (default 0).
    """
    return _call("get_balance", {"account_index": account_index})


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def get_address(account_index: Annotated[int, Field(ge=0)] = 0) -> str:
    """Return the primary address of an account.

    Args:
        account_index: Account to query (default 0).
    """
    return _call("get_address", {"account_index": account_index})


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def get_accounts() -> str:
    """Return the wallet's accounts (index, label, balance)."""
    return _call("get_accounts")


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def get_height() -> str:
    """Return the wallet's synced height."""
    return _call("get_height")


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def get_transfers(
    incoming: bool = True,
    outgoing: bool = True,
    pending: bool = True,
    failed: bool = True,
    pool: bool = True,
    account_index: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """Return the wallet's transfers.

    Args:
        incoming: Include received transfers (default True).
        outgoing: Include sent transfers (default True).
        pending: Include pending transfers (default True).
        failed: Include failed transfers (default True).
        pool: Include pool (unconfirmed) transfers (default True).
        account_index: Account to query (default 0).
    """
    return _call(
        "get_transfers",
        {
            "in": incoming,
            "out": outgoing,
            "pending": pending,
            "failed": failed,
            "pool": pool,
            "account_index": account_index,
        },
    )


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def get_transfer_by_txid(txid: str) -> str:
    """Return details of a transfer by its transaction id.

    Args:
        txid: Transaction id (hex).
    """
    return _call("get_transfer_by_txid", {"txid": txid})


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def get_payments(payment_id: str, account_index: Annotated[int, Field(ge=0)] = 0) -> str:
    """Return payments received with a given payment id.

    Args:
        payment_id: The payment id to look up (hex).
        account_index: Account to query (default 0).
    """
    return _call(
        "get_payments", {"payment_id": payment_id, "account_index": account_index}
    )


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def incoming_transfers(account_index: Annotated[int, Field(ge=0)] = 0) -> str:
    """Return the wallet's incoming transfers.

    Args:
        account_index: Account to query (default 0).
    """
    return _call("incoming_transfers", {"transfer_type": "all", "account_index": account_index})


# --- spend tools (registered only when ENABLE_SPENDING=1) -----------------


def _run_spend(
    action: str,
    amount: float,
    destination: str,
    params: dict[str, Any],
    dry_run: bool,
) -> str:
    """Shared spend path: precheck, reserve, call, audit, release on failure."""
    refusal = _precheck(amount, destination)
    if refusal is not None:
        _audit(action, amount, destination, f"REFUSED: {refusal}")
        return f"Refused: {refusal}"

    reserved = False
    if not dry_run:
        try:
            budget.reserve(amount)
            reserved = True
        except SpendRefused as err:
            _audit(action, amount, destination, f"REFUSED: {err}")
            return f"Refused: {err}"

    try:
        result = rpc.call(action, params)
    except RPCError as err:
        if reserved:
            budget.release(amount)
        _audit(action, amount, destination, f"FAILED: {err.message}")
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        if reserved:
            budget.release(amount)
        logger.exception("unexpected error in %s", action)
        return f"Unexpected error calling {action}: {err}"

    txid = result.get("tx_hash") if isinstance(result, dict) else None
    _audit(
        action,
        amount,
        destination,
        ("dry-run" if dry_run else "relayed") + (f" txid={txid}" if txid else ""),
    )
    return _render(result)


if ENABLE_SPENDING:

    @mcp.tool(structured_output=False, annotations=_WRITE_TOOL)
    def transfer(
        address: str,
        amount: float,
        dry_run: bool = True,
        priority: Literal["unimportant", "normal", "elevated", "priority"] = "normal",
        payment_id: str = "",
    ) -> str:
        """Send XMR to an address.

        Subject to the node's per-call and per-day caps and, if configured,
        an address allowlist. With dry_run=True (the default) the transfer is
        built and signed but not relayed to the network.

        Args:
            address: Destination Monero address.
            amount: Amount to send, in XMR (not atomic units).
            dry_run: Build and sign but do not relay (default True).
            priority: Transaction priority.
            payment_id: Optional payment id (hex).
        """
        atomic = int(round(amount * ATOMIC_PER_XMR))
        params: dict[str, Any] = {
            "destinations": [{"amount": atomic, "address": address}],
            "account_index": 0,
            "priority": _PRIORITY[priority],
            "get_tx_key": True,
            "do_not_relay": dry_run,
        }
        if payment_id:
            params["payment_id"] = payment_id
        return _run_spend("transfer", amount, address, params, dry_run)

    @mcp.tool(structured_output=False, annotations=_WRITE_TOOL)
    def sweep_all(
        address: str,
        dry_run: bool = True,
        priority: Literal["unimportant", "normal", "elevated", "priority"] = "normal",
    ) -> str:
        """Send the wallet's entire unlocked balance to an address.

        The spend amount is the wallet's current unlocked balance, checked
        against the caps. With dry_run=True (the default) the transfer is
        built and signed but not relayed.

        Args:
            address: Destination Monero address.
            dry_run: Build and sign but do not relay (default True).
            priority: Transaction priority.
        """
        try:
            balance = rpc.call("get_balance", {"account_index": 0}) or {}
        except RPCError as err:
            return _error_text(err)
        amount = float(balance.get("unlocked_balance", 0)) / ATOMIC_PER_XMR
        params: dict[str, Any] = {
            "address": address,
            "account_index": 0,
            "priority": _PRIORITY[priority],
            "do_not_relay": dry_run,
        }
        return _run_spend("sweep_all", amount, address, params, dry_run)

    @mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
    )
    def create_address(
        label: str = "", account_index: Annotated[int, Field(ge=0)] = 0
    ) -> str:
        """Create a new subaddress.

        Changes wallet state but moves no funds; still gated behind the spend
        token.

        Args:
            label: Optional label for the new address.
            account_index: Account to add the address to (default 0).
        """
        result = _call(
            "create_address", {"account_index": account_index, "label": label}
        )
        _audit("create_address", 0.0, "", label or "-")
        return result


class BearerAuthMiddleware:
    """Pure-ASGI bearer auth plus the spend-token gate.

    Two tokens: the read token admits the endpoint; the spend token is
    additionally required for any `tools/call` naming a spend tool. So a
    client handed only the read token cannot move funds even when spending is
    enabled.

    A missing read token is a startup error rather than a warning. GET /health
    stays unauthenticated and says nothing else.
    """

    def __init__(self, app: Any, read_token: str | None, spend_token: str | None) -> None:
        self.app = app
        self.read_token = read_token or ""
        self.spend_token = spend_token or ""
        if not self.read_token:
            if not MCP_ALLOW_ANONYMOUS:
                raise RuntimeError(
                    f"{TOKEN_ENV} is not set, and this endpoint is published on "
                    "the LAN. Set the token, or set MCP_ALLOW_ANONYMOUS=1 to "
                    "serve without authentication."
                )
            logger.warning(
                "%s not set and MCP_ALLOW_ANONYMOUS=1: endpoint is UNAUTHENTICATED",
                TOKEN_ENV,
            )
        if ENABLE_SPENDING and not self.spend_token:
            raise RuntimeError(
                f"ENABLE_SPENDING=1 requires {SPEND_TOKEN_ENV} to be set, so the "
                "read token alone cannot call the spend tools."
            )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == HEALTH_PATH:
            from starlette.responses import JSONResponse

            response = JSONResponse({"status": "ok"})
            await response(scope, receive, send)
            return

        presented = self._presented(scope)
        # Read access is granted by either token; the spend token is a
        # superset, so a spend-capable client can also read. In anonymous
        # mode there is no read token to check.
        read_ok = self._matches(presented, self.read_token) or self._matches(
            presented, self.spend_token
        )
        if self.read_token and not read_ok:
            await self._reject(send, 401, "unauthorized")
            return

        if scope.get("method") == "POST":
            # The MCP call is a single JSON-RPC body; read it to learn the
            # tool name, then replay it to the app.
            body = await _read_body(receive)
            if self._is_privileged(body) and not self._matches(presented, self.spend_token):
                await self._reject(send, 403, "this token may not call spend tools")
                return
            receive = _replay(body, receive)

        await self.app(scope, receive, send)

    def _is_privileged(self, body: bytes) -> bool:
        """True when a JSON-RPC body is a call to a spend tool."""
        try:
            payload = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return False
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            if not isinstance(message, dict) or message.get("method") != "tools/call":
                continue
            name = (message.get("params") or {}).get("name")
            if name in SPEND_TOOLS:
                return True
        return False

    def _presented(self, scope: Any) -> str:
        headers = dict(scope.get("headers") or [])
        header = headers.get(b"authorization", b"").decode("latin-1").strip()
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return ""
        return presented.strip()

    @staticmethod
    def _matches(presented: str, token: str) -> bool:
        if not token:
            return False
        return hmac.compare_digest(presented.encode(), token.encode())

    async def _reject(self, send: Any, status: int, message: str) -> None:
        from starlette.responses import JSONResponse

        response = JSONResponse({"error": message}, status_code=status)
        await response({"type": "http"}, _empty_receive, send)


async def _empty_receive() -> dict[str, Any]:
    return {"type": "http.disconnect"}


async def _read_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes, original: Any) -> Any:
    """A receive() that hands the already-read body to the app once, then
    delegates to the original -- so a later await still waits for the real
    disconnect rather than being told the client went away."""
    replayed = False

    async def receive() -> dict[str, Any]:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original()

    return receive


if __name__ == "__main__":
    import uvicorn

    app = BearerAuthMiddleware(
        mcp.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        ),
        os.environ.get(TOKEN_ENV),
        os.environ.get(SPEND_TOKEN_ENV),
    )

    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
    )
