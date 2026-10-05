"""Regression tests for Interactive Brokers snapshot tick selection."""

import pytest

from lumibot.brokers.interactive_brokers import IBWrapper


@pytest.mark.parametrize(
    "ticks",
    [
        [(4, 100.0), (9, 95.0)],
        [(9, 95.0), (4, 100.0)],
    ],
)
@pytest.mark.parametrize("should_use_last_close", [True, False])
def test_last_price_takes_precedence_over_previous_close(ticks, should_use_last_close, caplog):
    wrapper = IBWrapper()
    wrapper.init_tick()
    wrapper.should_use_last_close = should_use_last_close
    wrapper.tick_asset = "TEST"

    for tick_type, price in ticks:
        wrapper.tickPrice(1, tick_type, price, None)

    wrapper.tickSnapshotEnd(1)

    assert wrapper.my_tick_queue.get_nowait()["price"] == 100.0
    assert "Using yesterday's closing price" not in caplog.text


def test_previous_close_is_used_when_last_price_is_unavailable(caplog):
    wrapper = IBWrapper()
    wrapper.init_tick()
    wrapper.should_use_last_close = True
    wrapper.tick_asset = "TEST"

    wrapper.tickPrice(1, 9, 95.0, None)
    wrapper.tickSnapshotEnd(1)

    assert wrapper.my_tick_queue.get_nowait()["price"] == 95.0
    assert "Using yesterday's closing price of 95.0" in caplog.text


def test_previous_close_is_ignored_when_fallback_is_disabled():
    wrapper = IBWrapper()
    wrapper.init_tick()
    wrapper.should_use_last_close = False

    wrapper.tickPrice(1, 9, 95.0, None)
    wrapper.tickSnapshotEnd(1)

    assert wrapper.my_tick_queue.get_nowait()["price"] is None


def test_new_snapshot_resets_last_price_precedence(caplog):
    wrapper = IBWrapper()
    wrapper.should_use_last_close = True
    wrapper.init_tick()
    wrapper.tickPrice(1, 4, 100.0, None)
    wrapper.tickSnapshotEnd(1)
    assert wrapper.my_tick_queue.get_nowait()["price"] == 100.0

    wrapper.init_tick()
    wrapper.tick_asset = "TEST"
    wrapper.tickPrice(2, 9, 95.0, None)
    wrapper.tickSnapshotEnd(2)

    assert wrapper.my_tick_queue.get_nowait()["price"] == 95.0
    assert "Using yesterday's closing price of 95.0" in caplog.text


def test_last_price_precedence_preserves_bid_ask_snapshot():
    wrapper = IBWrapper()
    wrapper.init_tick()
    wrapper.should_use_last_close = True

    wrapper.tickPrice(1, 4, 100.0, None)
    wrapper.tickPrice(1, 1, 99.0, None)
    wrapper.tickPrice(1, 2, 101.0, None)
    wrapper.tickSize(1, 0, 10)
    wrapper.tickSize(1, 3, 20)
    wrapper.tickPrice(1, 9, 95.0, None)
    wrapper.tickSnapshotEnd(1)

    assert wrapper.my_tick_queue.get_nowait() == {
        "price": 100.0,
        "bid": 99.0,
        "ask": 101.0,
        "bid_size": 10,
        "ask_size": 20,
    }
