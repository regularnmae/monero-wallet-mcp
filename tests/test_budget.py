"""Unit tests for the daily spend budget."""

from __future__ import annotations

import pytest

from server import DailyBudget, SpendRefused


def test_reserve_release_and_refuse() -> None:
    b = DailyBudget(10.0, clock=lambda: 0.0)
    b.reserve(4.0)
    b.reserve(4.0)
    with pytest.raises(SpendRefused):
        b.reserve(4.0)
    b.release(4.0)
    b.reserve(4.0)  # room again


def test_counter_resets_on_a_new_day() -> None:
    now = [0.0]
    b = DailyBudget(5.0, clock=lambda: now[0])
    b.reserve(5.0)
    with pytest.raises(SpendRefused):
        b.reserve(0.01)
    now[0] += 86_400.0 * 2
    b.reserve(5.0)  # a fresh day


def test_release_never_goes_negative() -> None:
    b = DailyBudget(5.0, clock=lambda: 0.0)
    b.release(3.0)  # nothing was reserved
    b.reserve(5.0)  # the full budget is still available
