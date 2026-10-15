"""The spend path: caps, the allowlist, the budget, and dry runs.

`_run_spend` is the one function every spend tool funnels through, so it is
tested directly with a fake wallet and and an injected budget.
"""

from __future__ import annotations

import server
from server import DailyBudget, _precheck, _run_spend


def test_precheck_enforces_caps_and_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(server, "MAX_SPEND_PER_CALL", 5.0)
    monkeypatch.setattr(server, "ALLOWED_DESTINATIONS", frozenset({"allowed"}))
    assert _precheck(0.0, "allowed") is not None
    assert _precheck(6.0, "allowed") is not None
    assert _precheck(1.0, "other") is not None
    assert _precheck(1.0, "allowed") is None


def test_refusal_never_reaches_the_wallet(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(server.rpc, "call", lambda m, p=None: calls.append((m, p)) or {})
    monkeypatch.setattr(server, "MAX_SPEND_PER_CALL", 5.0)
    out = _run_spend("transfer", 10.0, "addr", {"x": 1}, dry_run=False)
    assert out.startswith("Refused:")
    assert calls == []


def test_dry_run_skips_the_budget(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        server.rpc, "call", lambda m, p=None: calls.append((m, p)) or {"tx_hash": "dead"}
    )
    monkeypatch.setattr(server, "MAX_SPEND_PER_CALL", 100.0)
    # A zero daily budget must still admit a dry run.
    monkeypatch.setattr(server, "budget", DailyBudget(0.0))
    out = _run_spend("transfer", 1.0, "addr", {"x": 1}, dry_run=True)
    assert "dead" in out
    assert calls == [("transfer", {"x": 1})]


def test_committed_spend_consumes_the_daily_budget(monkeypatch) -> None:
    monkeypatch.setattr(server.rpc, "call", lambda m, p=None: {"tx_hash": "beef"})
    monkeypatch.setattr(server, "MAX_SPEND_PER_CALL", 100.0)
    monkeypatch.setattr(server, "budget", DailyBudget(2.0))
    assert "beef" in _run_spend("transfer", 2.0, "addr", {}, dry_run=False)
    # The cap is now spent, so a second committed spend is refused.
    assert _run_spend("transfer", 1.0, "addr", {}, dry_run=False).startswith("Refused:")


def test_failed_spend_releases_the_reservation(monkeypatch) -> None:
    from rpc import RPCError

    def boom(m, p=None):
        raise RPCError("wallet busy", -1)

    monkeypatch.setattr(server.rpc, "call", boom)
    monkeypatch.setattr(server, "MAX_SPEND_PER_CALL", 100.0)
    budget = DailyBudget(1.0)
    monkeypatch.setattr(server, "budget", budget)
    assert "wallet busy" in _run_spend("transfer", 1.0, "addr", {}, dry_run=False)
    # The reservation was released, so the budget is available again.
    assert "wallet busy" in _run_spend("transfer", 1.0, "addr", {}, dry_run=False)
