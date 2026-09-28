"""Tests for :class:`lumibot.backtesting.InteractiveBrokersTWSBacktesting`.

Offline: the data source is driven with an injected fake IB client so the real
helper (cache, chunking, coverage, parsing) is exercised end to end without a
socket.
"""

from __future__ import annotations

import datetime as dt
import socket
from pathlib import Path

import pytest
import pytz

from lumibot.backtesting import InteractiveBrokersTWSBacktesting
from lumibot.entities import Asset
from lumibot.tools import ibkr_tws_helper as helper

from tests.test_ibkr_tws_helper import FakeBar, FakeClient, make_minute_bars

EASTERN = pytz.timezone("America/New_York")

START = dt.datetime(2024, 1, 3)
END = dt.datetime(2024, 1, 5)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "LUMIBOT_CACHE_FOLDER", str(tmp_path))
    return tmp_path


def minute_bars_for_range(first: dt.date, last: dt.date):
    sessions = helper.get_trading_sessions(
        dt.datetime.combine(first, dt.time.min), dt.datetime.combine(last, dt.time.min)
    )
    bars = []
    for session_date in sessions.index:
        bars.extend(make_minute_bars(session_date, 390))
    return bars


def day_bars_for_range(first: dt.date, last: dt.date):
    sessions = helper.get_trading_sessions(
        dt.datetime.combine(first, dt.time.min), dt.datetime.combine(last, dt.time.min)
    )
    return [
        FakeBar(d.strftime("%Y%m%d"), 100.0, 101.0, 99.0, 100.5, 1000) for d in sessions.index
    ]


def build_source(client, **kwargs):
    config = helper.IBKRTWSConfig(volume_multiplier=1.0)
    return InteractiveBrokersTWSBacktesting(
        datetime_start=START,
        datetime_end=END,
        config=config,
        client=client,
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------


def test_class_is_exported_from_the_backtesting_package():
    import lumibot.backtesting as bt

    assert "InteractiveBrokersTWSBacktesting" in bt.__all__
    assert bt.InteractiveBrokersTWSBacktesting is InteractiveBrokersTWSBacktesting


@pytest.mark.parametrize(
    "label", ["ibkr_tws", "interactive_brokers_tws"]
)
def test_backtesting_data_source_labels_resolve_to_the_tws_class(label):
    """The supported BACKTESTING_DATA_SOURCE labels map to the new class."""
    from lumibot.strategies import _strategy

    source = Path(_strategy.__file__).read_text()
    assert f'"{label}": "InteractiveBrokersTWSBacktesting"' in source
    assert _strategy._BACKTESTING_CLASS_MODULES["InteractiveBrokersTWSBacktesting"] == (
        "lumibot.backtesting.interactive_brokers_tws_backtesting"
    )


def test_plain_ibkr_label_still_means_the_rest_downloader():
    """Regression guard: the new source must not hijack the existing 'ibkr' label."""
    from lumibot.strategies import _strategy

    source = Path(_strategy.__file__).read_text()
    assert '"ibkr": "InteractiveBrokersRESTBacktesting"' in source
    assert _strategy._BACKTESTING_CLASS_MODULES["InteractiveBrokersRESTBacktesting"] == (
        "lumibot.backtesting.interactive_brokers_rest_backtesting"
    )


def test_prefers_native_day_bars():
    assert InteractiveBrokersTWSBacktesting.PREFER_NATIVE_DAY_BARS_FOR_STOCK_INDEX is True


# --------------------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------------------


def test_update_pandas_data_populates_the_store(cache_dir):
    client = FakeClient(default=minute_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5)))
    source = build_source(client)
    try:
        source._update_pandas_data(Asset("SPY"), None, 1, "minute", START)
        assert source.pandas_data, "no dataset was loaded"
        data = next(iter(source.pandas_data.values()))
        assert data.timestep == "minute"
        assert not data.df.empty
        assert client.request_count > 0
    finally:
        source.close()


def test_two_assets_share_one_connection(cache_dir):
    bars = minute_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5))
    client = FakeClient(default=bars)
    source = build_source(client)
    try:
        source._update_pandas_data(Asset("SPY"), None, 1, "minute", START)
        source._update_pandas_data(Asset("QQQ"), None, 1, "minute", START)
        assert len({k[0].symbol for k in source.pandas_data}) == 2
        assert source._client is client
    finally:
        source.close()


def test_warm_cache_makes_no_requests_on_a_second_source(cache_dir):
    bars = minute_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5))
    first = build_source(FakeClient(default=bars))
    try:
        first._update_pandas_data(Asset("SPY"), None, 1, "minute", START)
    finally:
        first.close()

    warm_client = FakeClient(default=[])
    second = build_source(warm_client)
    try:
        second._update_pandas_data(Asset("SPY"), None, 1, "minute", START)
        assert warm_client.request_count == 0
        assert second.pandas_data
    finally:
        second.close()


def test_minute_and_day_datasets_coexist_in_both_load_orders(cache_dir):
    minute = minute_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5))
    day = day_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5))

    def responses(client_bars):
        return FakeClient(default=client_bars)

    for order in (("minute", "day"), ("day", "minute")):
        source = build_source(FakeClient(default=[]))
        try:
            for step in order:
                source._client = responses(minute if step == "minute" else day)
                source._update_pandas_data(Asset("SPY"), None, 1, step, START)
            timesteps = {d.timestep for d in source.pandas_data.values()}
            assert timesteps == {"minute", "day"}, f"order={order} produced {timesteps}"
        finally:
            source.close()


def test_day_request_selects_the_native_day_dataset(cache_dir):
    source = build_source(FakeClient(default=[]))
    try:
        source._client = FakeClient(default=minute_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5)))
        source._update_pandas_data(Asset("SPY"), None, 1, "minute", START)
        source._client = FakeClient(default=day_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5)))
        source._update_pandas_data(Asset("SPY"), None, 1, "day", START)

        found = source.find_asset_in_data_store(Asset("SPY"), None, timestep="day")
        assert found is not None
        assert source.pandas_data[found].timestep == "day"
    finally:
        source.close()


def test_day_bars_are_stamped_at_the_session_close(cache_dir):
    """A daily backtest must not see the current session's bar at the open."""
    client = FakeClient(default=day_bars_for_range(dt.date(2023, 12, 20), dt.date(2024, 1, 5)))
    source = build_source(client)
    try:
        source._update_pandas_data(Asset("SPY"), None, 1, "day", START)
        data = next(iter(source.pandas_data.values()))
        hours = {ts.tz_convert(EASTERN).hour for ts in data.df.index}
        assert hours == {16}
    finally:
        source.close()


def test_unsupported_asset_type_propagates_not_implemented(cache_dir):
    source = build_source(FakeClient(default=[]))
    option = Asset(
        "SPY", asset_type=Asset.AssetType.OPTION, expiration=dt.date(2024, 1, 19), strike=400, right="CALL"
    )
    try:
        with pytest.raises(NotImplementedError):
            source._update_pandas_data(option, None, 1, "minute", START)
    finally:
        source.close()


def test_get_chains_is_not_implemented(cache_dir):
    source = build_source(FakeClient(default=[]))
    try:
        with pytest.raises(NotImplementedError) as excinfo:
            source.get_chains(Asset("SPY"))
        assert "Option chains are not supported" in str(excinfo.value)
    finally:
        source.close()


def test_get_last_price_surfaces_transport_failures(cache_dir):
    """A download failure must not be downgraded to a stale or missing price.

    Swallowing it would let the backtest price off whatever happened to be loaded
    already, which silently corrupts results.
    """
    source = build_source(FakeClient(default=helper.IBKRTWSTimeoutError("nope")))
    try:
        with pytest.raises(Exception) as excinfo:
            source.get_last_price(Asset("SPY"), timestep="minute")
        assert "IB Gateway/TWS" in str(excinfo.value)
    finally:
        source.close()


def test_get_last_price_propagates_unsupported_asset_types(cache_dir):
    source = build_source(FakeClient(default=[]))
    option = Asset(
        "SPY", asset_type=Asset.AssetType.OPTION, expiration=dt.date(2024, 1, 19), strike=400, right="CALL"
    )
    try:
        with pytest.raises(NotImplementedError):
            source.get_last_price(option, timestep="minute")
    finally:
        source.close()


# --------------------------------------------------------------------------------------
# Configuration / lifecycle
# --------------------------------------------------------------------------------------


def test_kwargs_override_environment(monkeypatch):
    monkeypatch.setenv("INTERACTIVE_BROKERS_PORT", "7497")
    monkeypatch.setenv("IBKR_BACKTEST_CLIENT_ID", "88")
    source = InteractiveBrokersTWSBacktesting(
        datetime_start=START, datetime_end=END, port=4001, client=FakeClient()
    )
    try:
        assert source.ibkr_config.port == 4001
        assert source.ibkr_config.client_id == 88
    finally:
        source.close()


def test_environment_is_used_when_no_kwargs(monkeypatch):
    monkeypatch.setenv("INTERACTIVE_BROKERS_IP", "10.1.2.3")
    monkeypatch.setenv("INTERACTIVE_BROKERS_PORT", "7496")
    source = InteractiveBrokersTWSBacktesting(
        datetime_start=START, datetime_end=END, client=FakeClient()
    )
    try:
        assert source.ibkr_config.host == "10.1.2.3"
        assert source.ibkr_config.port == 7496
    finally:
        source.close()


def test_an_injected_client_is_not_closed_by_the_source():
    client = FakeClient()
    source = InteractiveBrokersTWSBacktesting(datetime_start=START, datetime_end=END, client=client)
    source.close()
    assert client.closed is False


def test_an_owned_client_is_closed_by_the_source(monkeypatch):
    created = []

    class Recorder(FakeClient):
        def __init__(self, config):
            super().__init__(default=[])
            created.append(self)

    monkeypatch.setattr(
        "lumibot.backtesting.interactive_brokers_tws_backtesting.IBKRTWSClient", Recorder
    )
    source = InteractiveBrokersTWSBacktesting(datetime_start=START, datetime_end=END)
    client = source._get_client()
    assert client is created[0]
    source.close()
    assert created[0].closed is True
    # Idempotent.
    source.close()


def test_context_manager_closes_the_client(monkeypatch):
    created = []

    class Recorder(FakeClient):
        def __init__(self, config):
            super().__init__(default=[])
            created.append(self)

    monkeypatch.setattr(
        "lumibot.backtesting.interactive_brokers_tws_backtesting.IBKRTWSClient", Recorder
    )
    with InteractiveBrokersTWSBacktesting(datetime_start=START, datetime_end=END) as source:
        source._get_client()
    assert created[0].closed is True


def test_no_client_is_created_when_nothing_is_requested():
    source = InteractiveBrokersTWSBacktesting(datetime_start=START, datetime_end=END)
    try:
        assert source._client is None
    finally:
        source.close()


# --------------------------------------------------------------------------------------
# Optional live smoke test
# --------------------------------------------------------------------------------------


def _gateway_reachable() -> bool:
    import os

    if os.environ.get("IBKR_TWS_LIVE_TEST", "").strip().lower() not in {"1", "true", "yes"}:
        return False
    config = helper.IBKRTWSConfig.from_env()
    try:
        with socket.create_connection((config.host, config.port), timeout=2):
            return True
    except OSError:
        return False


@pytest.mark.apitest
@pytest.mark.skipif(
    not _gateway_reachable(),
    reason="Set IBKR_TWS_LIVE_TEST=1 and point INTERACTIVE_BROKERS_IP/PORT at a reachable gateway",
)
def test_live_gateway_minute_download(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "LUMIBOT_CACHE_FOLDER", str(tmp_path))
    end = dt.datetime.now() - dt.timedelta(days=7)
    start = end - dt.timedelta(days=3)
    source = InteractiveBrokersTWSBacktesting(datetime_start=start, datetime_end=end)
    try:
        df = helper.get_price_data_from_ibkr_tws(
            Asset("SPY"),
            start,
            end,
            timespan="minute",
            config=source.ibkr_config,
            client=source._get_client(),
            show_progress=False,
        )
        assert not df.empty
        assert {"open", "high", "low", "close", "volume"}.issubset(df.columns)
    finally:
        source.close()
