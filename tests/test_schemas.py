"""What the server advertises in `tools/list`.

With spending disabled (the default), only the read tools are registered --
there is no way to reach a spend tool at all. Each read tool is text-only, so
each must pass `structured_output=False`.
"""

from __future__ import annotations

import anyio
import pytest
from mcp import Client

import server

READ_TOOLS = {
    "get_balance",
    "get_address",
    "get_accounts",
    "get_height",
    "get_transfers",
    "get_transfer_by_txid",
    "get_payments",
    "incoming_transfers",
}

SPEND_TOOLS = {"transfer", "sweep_all", "create_address"}


@pytest.fixture
def advertised() -> dict:
    async def _list():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    listing = anyio.run(_list)
    return {tool.name: tool for tool in listing.tools}


def test_only_read_tools_are_registered_by_default(advertised: dict) -> None:
    assert set(advertised) == READ_TOOLS
    # Spending is off in the test environment, so the spend tools must not
    # exist at all.
    assert SPEND_TOOLS & set(advertised) == set()


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
def test_text_tools_publish_no_output_schema(advertised: dict, name: str) -> None:
    assert advertised[name].output_schema is None, (
        f"{name} publishes an output schema; pass structured_output=False"
    )


@pytest.mark.parametrize(
    "name,field",
    [
        ("get_balance", "account_index"),
        ("get_address", "account_index"),
        ("get_transfers", "account_index"),
        ("get_payments", "account_index"),
        ("incoming_transfers", "account_index"),
    ],
)
def test_account_index_is_bounded(advertised: dict, name: str, field: str) -> None:
    schema = advertised[name].input_schema["properties"][field]
    assert schema.get("minimum") == 0, f"{name}.{field} has no lower bound"


def test_every_default_tool_is_read_only(advertised: dict) -> None:
    for name, tool in advertised.items():
        assert tool.annotations is not None, f"{name} publishes no annotations"
        assert tool.annotations.read_only_hint is True, f"{name} is not marked read-only"


def test_spend_tool_names_match_the_gate() -> None:
    assert server.SPEND_TOOLS == SPEND_TOOLS
