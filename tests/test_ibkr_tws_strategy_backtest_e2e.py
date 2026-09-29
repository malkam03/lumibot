"""End-to-end ``Strategy.backtest()`` regressions for the IB Gateway/TWS source.

Why this file exists: unit tests once passed while every real backtest on this
source was broken. ``_update_pandas_data()`` stored datasets under
``(asset, quote, timestep)`` keys, but the inherited ``PandasData.get_last_price()``
looks them up *without* a timestep, so the lookup always missed,
``get_last_price()`` always returned ``None``, and no order could ever fill.

These tests deliberately mock nothing above the socket: a fake IB client feeds
the real helper (parsing, coverage, parquet cache), the real data source storage,
the real ``find_asset_in_data_store`` lookup, the real backtesting broker, and a
real ``Strategy.backtest()`` run. They assert that an order actually fills at a
real price from the fake bars.
"""

from __future__ import annotations

import datetime as dt

import pytest

from lumibot.backtesting import InteractiveBrokersTWSBacktesting
from lumibot.strategies.strategy import Strategy
from lumibot.tools import ibkr_tws_helper as helper

from tests.test_ibkr_tws_backtesting import day_bars_for_range, minute_bars_for_range
from tests.test_ibkr_tws_helper import FakeClient

START = dt.datetime(2024, 1, 3)
END = dt.datetime(2024, 1, 6)
# make_minute_bars()/day_bars_for_range() only ever emit prices in this band, so a
# fill inside it proves the price came from the loaded IB bars.
PRICE_BAND = (99.0, 105.0)


class BarSizeAwareFakeClient(FakeClient):
    """Returns minute or day bars depending on the requested IB bar size."""

    def __init__(self, minute_bars, day_bars):
        super().__init__(default=[])
        self._minute_bars = minute_bars
        self._day_bars = day_bars

    def request_historical_bars(self, contract, end_datetime, duration, bar_size, **kwargs):
        super().request_historical_bars(contract, end_datetime, duration, bar_size, **kwargs)
        return list(self._day_bars if "day" in bar_size else self._minute_bars)


class BuyOnceStrategy(Strategy):
    """Buys once and records every price the strategy saw."""

    observed_prices: list = []
    fills: list = []

    def initialize(self, parameters=None):
        self.sleeptime = self.parameters.get("sleeptime", "1M")
        self.set_market("NYSE")
        self._bought = False

    def on_trading_iteration(self):
        price = self.get_last_price("SPY")
        type(self).observed_prices.append(price)
        if not self._bought and price is not None:
            self.submit_order(self.create_order("SPY", 10, "buy"))
            self._bought = True

    def on_filled_order(self, position, order, price, quantity, multiplier):
        type(self).fills.append((order.side, float(price), float(quantity)))


@pytest.fixture
def fake_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "LUMIBOT_CACHE_FOLDER", str(tmp_path))
    monkeypatch.setenv("BACKTESTING_DATA_SOURCE", "none")
    created = []

    def factory(config):
        client = BarSizeAwareFakeClient(
            minute_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5)),
            day_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5)),
        )
        created.append(client)
        return client

    monkeypatch.setattr(
        "lumibot.backtesting.interactive_brokers_tws_backtesting.IBKRTWSClient", factory
    )
    BuyOnceStrategy.observed_prices = []
    BuyOnceStrategy.fills = []
    return created


def _run_backtest(sleeptime):
    return BuyOnceStrategy.backtest(
        InteractiveBrokersTWSBacktesting,
        START,
        END,
        parameters={"sleeptime": sleeptime},
        benchmark_asset=None,
        show_plot=False,
        show_tearsheet=False,
        save_tearsheet=False,
        save_stats_file=False,
        show_progress_bar=False,
        show_indicators=False,
        save_logfile=False,
    )


def _assert_filled_at_a_real_price():
    real_prices = [p for p in BuyOnceStrategy.observed_prices if p is not None]
    assert real_prices, "get_last_price() never returned a price"
    assert all(PRICE_BAND[0] <= float(p) <= PRICE_BAND[1] for p in real_prices)

    buys = [fill for fill in BuyOnceStrategy.fills if str(fill[0]).lower() == "buy"]
    assert len(buys) == 1, f"expected exactly one buy fill, got {BuyOnceStrategy.fills}"
    _, fill_price, fill_qty = buys[0]
    assert fill_qty == 10
    assert PRICE_BAND[0] <= fill_price <= PRICE_BAND[1]


def test_minute_backtest_prices_and_fills_an_order(fake_gateway):
    _run_backtest("1M")

    # The very first iteration must already see a price: the regression made it
    # None on *every* iteration.
    assert BuyOnceStrategy.observed_prices, "the strategy never iterated"
    assert BuyOnceStrategy.observed_prices[0] is not None
    _assert_filled_at_a_real_price()
    assert fake_gateway and fake_gateway[0].request_count >= 1


def test_daily_backtest_prices_from_native_day_bars_and_fills_an_order(fake_gateway):
    _run_backtest("1D")

    _assert_filled_at_a_real_price()
    # Opting into the daily last-price shortcut makes the strategy's own
    # get_last_price() read native day bars. (Portfolio valuation and fills still
    # use the source's minute default; see the docs' "Daily-cadence strategies".)
    bar_sizes = {request["bar_size"] for client in fake_gateway for request in client.requests}
    assert any("day" in size for size in bar_sizes), bar_sizes


def test_unspecified_timestep_lookup_reaches_timestep_keyed_datasets(fake_gateway):
    """The exact lookup ``PandasData.get_last_price()`` performs must resolve."""
    source = InteractiveBrokersTWSBacktesting(datetime_start=START, datetime_end=END)
    # The broker only runs its fill model for SOURCE == "PANDAS"; see the class comment.
    assert source.SOURCE == "PANDAS"
    try:
        source._datetime = source.to_default_timezone(dt.datetime(2024, 1, 4, 11, 0))
        from lumibot.entities import Asset

        spy = Asset("SPY")
        source._update_pandas_data(spy, None, 1, "minute", source.get_datetime())
        key = source.find_asset_in_data_store(spy, None)
        assert key is not None and key[2] == "minute"

        # A later day load must not shadow the finer minute dataset for untyped lookups.
        source._update_pandas_data(spy, None, 1, "day", source.get_datetime())
        assert source.find_asset_in_data_store(spy, None)[2] == "minute"
        assert source.find_asset_in_data_store(spy, None, "day")[2] == "day"
    finally:
        source.close()


class _PlainPandasLikeSource:
    """A source whose class name matches none of the legacy substrings."""


class _OptedInSource:
    SUPPORTS_DAILY_LAST_PRICE_OPTIMIZATION = True


@pytest.mark.parametrize(
    ("data_source", "expected"),
    [
        (InteractiveBrokersTWSBacktesting.__new__(InteractiveBrokersTWSBacktesting), True),
        (_OptedInSource(), True),
        (_PlainPandasLikeSource(), False),
        (None, False),
    ],
)
def test_daily_last_price_optimization_opt_in(data_source, expected):
    from types import SimpleNamespace

    fake_strategy = SimpleNamespace(broker=SimpleNamespace(data_source=data_source))
    assert Strategy._supports_daily_last_price_optimization(fake_strategy) is expected
