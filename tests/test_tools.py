"""Read-tool behaviour against a scripted wallet."""

from __future__ import annotations

import json

import pytest

from rpc import RPCError


@pytest.mark.anyio
async def test_get_balance_forwards_account_index(call, fake_rpc) -> None:
    fake_rpc.answers["get_balance"] = {"balance": 5, "unlocked_balance": 4}
    text = await call("get_balance", account_index=2)
    assert json.loads(text)["unlocked_balance"] == 4
    assert fake_rpc.calls == [("get_balance", {"account_index": 2})]


@pytest.mark.anyio
async def test_get_transfers_sets_every_source_flag(call, fake_rpc) -> None:
    await call("get_transfers")
    assert fake_rpc.calls == [
        (
            "get_transfers",
            {
                "in": True,
                "out": True,
                "pending": True,
                "failed": True,
                "pool": True,
                "account_index": 0,
            },
        )
    ]


@pytest.mark.anyio
async def test_get_transfer_by_txid_forwards_hash(call, fake_rpc) -> None:
    await call("get_transfer_by_txid", txid="ab" * 32)
    assert fake_rpc.calls == [("get_transfer_by_txid", {"txid": "ab" * 32})]


@pytest.mark.anyio
async def test_unknown_error_is_surfaced_with_its_code(call, fake_rpc) -> None:
    fake_rpc.answers["get_height"] = RPCError("no wallet open", -1)
    text = await call("get_height")
    assert "no wallet open" in text
    assert "-1" in text


@pytest.mark.anyio
async def test_transport_error_has_no_code(call, fake_rpc) -> None:
    fake_rpc.answers["get_accounts"] = RPCError("cannot reach the wallet at http://x")
    text = await call("get_accounts")
    assert "cannot reach the wallet" in text
