"""Shared SQL deadline and expired recovery budget guards; no real PG here."""

import pytest

from scripts import sender_drain_inventory as inventory
from scripts.durable_handoff_cutover import CutoverError


def test_sql_timeout_uses_remaining_overall_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])

    class Cursor:
        def __init__(self):
            self.timeouts = []
            self.queries = []

        def execute(self, sql, params=None):
            if sql.startswith("SELECT set_config"):
                self.timeouts.append(int(params[0]))
                return
            self.queries.append(sql)
            duration = min(2.4, self.timeouts[-1] / 1000)
            clock[0] += duration
            if duration < 2.4:
                raise TimeoutError(
                    "synthetic server enforces remaining statement timeout"
                )

    raw = Cursor()
    cursor = inventory._TimedCursor(raw, 110.0)
    for _ in range(4):
        cursor.execute("SELECT synthetic")
    with pytest.raises(TimeoutError):
        cursor.execute("SELECT final")
    assert raw.timeouts[:3] == [3000, 3000, 3000]
    assert 2798 <= raw.timeouts[3] <= 2800 and 398 <= raw.timeouts[4] <= 400
    assert clock[0] <= 110.0
    queries = len(raw.queries)
    clock[0] = 110.001
    with pytest.raises(CutoverError):
        cursor.execute("SELECT too_late")
    assert len(raw.queries) == queries


def test_connection_minimum_cannot_restart_a_nearly_expired_budget(monkeypatch):
    monkeypatch.setattr(inventory.time, "monotonic", lambda: 10.0)
    calls = []
    with pytest.raises(CutoverError):
        inventory._connect(
            lambda *args, **kwargs: calls.append((args, kwargs)), "synthetic", 11.9
        )
    assert calls == []
    assert inventory._deadline(10.5) == 10.5
    assert inventory._deadline(1000) == 20.0


@pytest.mark.parametrize("row", [None, ("a" * 64, "pause", 3, 4, "pending", False),
                                   ("a" * 64, "stop_new", 3, 4, "confirmed", True),
                                   ("a" * 64, "pause", 2, 3, "confirmed", True)])
def test_closed_state_alone_does_not_prove_confirmed_pause(row):
    held = inventory.FrozenPostgres(None, "b" * 64, "a" * 64)
    held.snapshot = {"gate": {"generation": 4}}
    class Cursor:
        def execute(self, sql, params):
            assert params == ("b" * 64,)
        def fetchone(self):
            return row
    with pytest.raises(CutoverError, match="pause operation"):
        held._confirmed_operation(Cursor())
