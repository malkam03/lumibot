"""Unit tests for :mod:`lumibot.tools.ibkr_tws_helper`.

Everything here runs offline against a fake ibapi client. The behaviours these
tests pin down are the ones that make an IB socket download safe to cache:

* bar parsing and timezone handling (including the "day bars land on the session
  close, not midnight" rule that prevents same-session lookahead)
* session-level coverage, so a response truncated at IB's bar cap is retried
  rather than silently cached
* only authoritative, successful requests may write negative cache markers
* a warm cache performs zero network calls and never constructs a client
* pacing rules and IB error classification
"""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

import pandas as pd
import pytest
import pytz

from lumibot.entities import Asset
from lumibot.tools import ibkr_tws_helper as helper

EASTERN = pytz.timezone("America/New_York")


# --------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------


class FakeBar:
    """Stand-in for ``ibapi.common.BarData``."""

    def __init__(self, date, open_, high, low, close, volume):
        self.date = date
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class FakeContract:
    def __init__(self, symbol="SPY", sec_type="STK", exchange="SMART", currency="USD"):
        self.symbol = symbol
        self.secType = sec_type
        self.exchange = exchange
        self.currency = currency


class FakeClient:
    """Records requests and replays canned responses.

    ``responses`` maps the *end date* (``datetime.date``) of a request onto either
    a list of bars, or an exception instance to raise.
    """

    def __init__(self, responses=None, default=None):
        self.responses = responses or {}
        self.default = default if default is not None else []
        self.requests = []
        self.request_count = 0
        self.closed = False

    def request_historical_bars(self, contract, end_datetime, duration, bar_size, **kwargs):
        self.request_count += 1
        self.requests.append(
            {
                "symbol": contract.symbol,
                "end": end_datetime,
                "duration": duration,
                "bar_size": bar_size,
                **kwargs,
            }
        )
        key = end_datetime.astimezone(EASTERN).date()
        result = self.responses.get(key, self.default)
        if isinstance(result, Exception):
            raise result
        return list(result)

    def close(self):
        self.closed = True


class ExplodingClientFactory:
    """Fails the test if a client is ever constructed (warm-cache guard)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, config):
        self.calls += 1
        raise AssertionError("A client must not be created when the cache is warm")


def make_minute_bars(session_date: dt.date, count: int, *, start_hour=9, start_minute=30):
    """``count`` consecutive 1-minute bars starting at the session open."""
    base = EASTERN.localize(
        dt.datetime(session_date.year, session_date.month, session_date.day, start_hour, start_minute)
    )
    bars = []
    for i in range(count):
        stamp = base + dt.timedelta(minutes=i)
        epoch = int(stamp.astimezone(dt.timezone.utc).timestamp())
        price = 100.0 + i * 0.01
        bars.append(FakeBar(epoch, price, price + 0.05, price - 0.05, price + 0.01, 10 + i))
    return bars


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "LUMIBOT_CACHE_FOLDER", str(tmp_path))
    return tmp_path


@pytest.fixture
def config():
    return helper.IBKRTWSConfig(volume_multiplier=1.0)


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asset",
    [
        Asset("SPY", asset_type=Asset.AssetType.OPTION, expiration=dt.date(2024, 1, 19), strike=400, right="CALL"),
        Asset("ES", asset_type=Asset.AssetType.FUTURE, expiration=dt.date(2024, 3, 15)),
        Asset("BTC", asset_type=Asset.AssetType.CRYPTO),
        Asset("EUR", asset_type=Asset.AssetType.FOREX),
    ],
)
def test_unsupported_asset_types_raise_not_implemented(asset):
    with pytest.raises(NotImplementedError) as excinfo:
        helper.validate_asset(asset)
    assert "does not support asset type" in str(excinfo.value)


def test_stock_asset_is_supported():
    helper.validate_asset(Asset("SPY"))  # must not raise


@pytest.mark.parametrize("timespan", ["hour", "second", "1min", "week", "", None])
def test_unsupported_timespans_raise_value_error(timespan):
    with pytest.raises(ValueError) as excinfo:
        helper.validate_timespan(timespan)
    assert "Unsupported timestep" in str(excinfo.value)


def test_supported_timespans_map_to_ib_bar_sizes():
    assert helper.validate_timespan("minute") == "1 min"
    assert helper.validate_timespan("DAY") == "1 day"


def test_get_price_data_rejects_options_before_touching_the_network(cache_dir, config):
    option = Asset("SPY", asset_type=Asset.AssetType.OPTION, expiration=dt.date(2024, 1, 19), strike=400, right="CALL")
    factory = ExplodingClientFactory()
    with pytest.raises(NotImplementedError):
        helper.get_price_data_from_ibkr_tws(
            option,
            dt.datetime(2024, 1, 2),
            dt.datetime(2024, 1, 3),
            timespan="minute",
            config=config,
            client_factory=factory,
        )
    assert factory.calls == 0


# --------------------------------------------------------------------------------------
# Bar / timestamp parsing
# --------------------------------------------------------------------------------------


def test_parse_ib_datetime_epoch_seconds():
    stamp = EASTERN.localize(dt.datetime(2024, 1, 3, 9, 30))
    epoch = int(stamp.astimezone(dt.timezone.utc).timestamp())
    parsed = helper.parse_ib_datetime(epoch, "minute")
    assert parsed == pd.Timestamp("2024-01-03 14:30:00", tz="UTC")
    assert parsed.tz is not None


def test_parse_ib_datetime_epoch_string():
    assert helper.parse_ib_datetime("1704292200", "minute") == pd.Timestamp(
        "2024-01-03 14:30:00", tz="UTC"
    )


def test_parse_ib_datetime_naive_string_is_eastern():
    parsed = helper.parse_ib_datetime("20240103 09:30:00", "minute")
    assert parsed == pd.Timestamp("2024-01-03 14:30:00", tz="UTC")


def test_parse_ib_datetime_string_with_timezone_suffix():
    assert helper.parse_ib_datetime("20240103 14:30:00 UTC", "minute") == pd.Timestamp(
        "2024-01-03 14:30:00", tz="UTC"
    )
    assert helper.parse_ib_datetime("20240103 09:30:00 America/New_York", "minute") == pd.Timestamp(
        "2024-01-03 14:30:00", tz="UTC"
    )


def test_parse_ib_datetime_dst_boundary_minute_bars():
    """Same wall-clock open maps to different UTC times across the DST switch."""
    winter = helper.parse_ib_datetime("20240301 09:30:00", "minute")
    summer = helper.parse_ib_datetime("20240701 09:30:00", "minute")
    assert winter.hour == 14  # EST -> UTC-5
    assert summer.hour == 13  # EDT -> UTC-4


def test_day_bars_are_stamped_at_the_session_close_not_midnight():
    """Regression guard against same-session lookahead for daily backtests."""
    sessions = helper.get_trading_sessions(dt.datetime(2024, 1, 8), dt.datetime(2024, 1, 8))
    closes = helper._session_close_lookup(sessions)
    parsed = helper.parse_ib_datetime("20240108", "day", closes)
    assert parsed == pd.Timestamp("2024-01-08 21:00:00", tz="UTC")  # 16:00 ET
    assert parsed.tz_convert(EASTERN).hour == 16
    # A strategy stepping at 09:30 ET must not yet see this bar.
    open_et = EASTERN.localize(dt.datetime(2024, 1, 8, 9, 30))
    assert parsed > pd.Timestamp(open_et)


def test_day_bars_respect_nyse_early_closes():
    """Half sessions (e.g. the day after Thanksgiving) close at 13:00 ET."""
    sessions = helper.get_trading_sessions(dt.datetime(2024, 11, 29), dt.datetime(2024, 11, 29))
    closes = helper._session_close_lookup(sessions)
    parsed = helper.parse_ib_datetime("20241129", "day", closes)
    assert parsed.tz_convert(EASTERN).hour == 13


def test_day_bars_fall_back_to_1600_et_when_the_session_is_unknown():
    parsed = helper.parse_ib_datetime("20240108", "day", {})
    assert parsed.tz_convert(EASTERN).hour == 16


def test_parse_bars_frame_shape_and_dtypes():
    bars = make_minute_bars(dt.date(2024, 1, 3), 3)
    df = helper.parse_bars(bars, "minute")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "datetime"
    assert str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing
    assert len(df) == 3


def test_parse_bars_applies_the_volume_multiplier():
    bars = make_minute_bars(dt.date(2024, 1, 3), 1)
    df = helper.parse_bars(bars, "minute", volume_multiplier=100.0)
    assert df["volume"].iloc[0] == 1000.0  # 10 lots -> 1000 shares


def test_parse_bars_drops_all_zero_rows():
    """IB pads halted intervals with zeros; those are not real prices (RULE #1)."""
    bars = make_minute_bars(dt.date(2024, 1, 3), 2)
    bars.append(FakeBar(bars[-1].date + 60, 0.0, 0.0, 0.0, 0.0, 0))
    df = helper.parse_bars(bars, "minute")
    assert len(df) == 2
    assert (df[["open", "high", "low", "close"]] != 0).all().all()


def test_parse_bars_empty_input_returns_empty_frame():
    df = helper.parse_bars([], "minute")
    assert df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_stock_trades_volume_multiplier_preserves_api_units_by_default():
    cfg = helper.IBKRTWSConfig()
    assert cfg.effective_volume_multiplier(Asset("SPY")) == 1.0
    assert cfg.replace(volume_multiplier=1.0).effective_volume_multiplier(Asset("SPY")) == 1.0
    assert cfg.replace(volume_multiplier=100.0).effective_volume_multiplier(Asset("SPY")) == 100.0
    bar = make_minute_bars(dt.date(2024, 1, 3), 1)
    assert helper.parse_bars(bar, "minute")["volume"].iloc[0] == 10.0


@pytest.mark.parametrize("timespan", ["minute", "day"])
def test_in_progress_session_is_always_missing_even_with_sufficient_data(timespan):
    session_date = dt.date(2024, 1, 3)
    start = end = dt.datetime.combine(session_date, dt.time.min)
    sessions = helper.get_trading_sessions(start, end)
    if timespan == "minute":
        bars = make_minute_bars(session_date, 360)
    else:
        bars = [FakeBar("20240103", 100, 101, 99, 100, 10)]
    df = helper.parse_bars(bars, timespan)

    before_close = EASTERN.localize(dt.datetime(2024, 1, 3, 15, 45))
    after_close = EASTERN.localize(dt.datetime(2024, 1, 3, 16, 1))
    assert helper.compute_missing_sessions(df, sessions, timespan, now=before_close) == [session_date]
    assert helper.compute_missing_sessions(df, sessions, timespan, now=after_close) == []


# --------------------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------------------


def test_build_chunks_groups_contiguous_sessions():
    sessions = [dt.date(2024, 1, d) for d in (2, 3, 4, 5)]
    assert helper.build_chunks(sessions, "minute", 14) == [(dt.date(2024, 1, 2), dt.date(2024, 1, 5))]


def test_build_chunks_splits_on_the_chunk_day_limit():
    sessions = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(0, 40, 2)]
    chunks = helper.build_chunks(sessions, "minute", 14)
    assert len(chunks) > 1
    for first, last in chunks:
        assert (last - first).days + 1 <= 14
    # Every session must still be covered exactly once.
    covered = [d for first, last in chunks for d in sessions if first <= d <= last]
    assert sorted(covered) == sorted(sessions)


def test_build_chunks_single_session():
    assert helper.build_chunks([dt.date(2024, 1, 2)], "minute", 14) == [
        (dt.date(2024, 1, 2), dt.date(2024, 1, 2))
    ]


def test_build_chunks_empty():
    assert helper.build_chunks([], "minute", 14) == []


def test_build_chunks_rejects_bad_chunk_days():
    with pytest.raises(ValueError):
        helper.build_chunks([dt.date(2024, 1, 2)], "minute", 0)


def test_format_duration_uses_days_then_years():
    assert helper.format_duration(dt.date(2024, 1, 1), dt.date(2024, 1, 14)) == "15 D"
    assert helper.format_duration(dt.date(2024, 1, 1), dt.date(2024, 1, 1)) == "2 D"
    assert helper.format_duration(dt.date(2020, 1, 1), dt.date(2024, 1, 1)).endswith("Y")


def test_maximum_calendar_chunk_stays_one_year_after_slack():
    first = dt.date(2024, 1, 1)
    last = first + dt.timedelta(days=364)
    assert helper.format_duration(first, last) == "1 Y"


def test_minute_chunk_default_stays_under_the_ib_bar_cap():
    """A "1 M" minute request returns exactly 8190 bars -- right at IB's cap."""
    max_sessions = helper.DEFAULT_MINUTE_CHUNK_DAYS * 5 / 7
    assert max_sessions * 390 < 8190


# --------------------------------------------------------------------------------------
# Missing-session computation
# --------------------------------------------------------------------------------------


def _sessions(start, end):
    return helper.get_trading_sessions(start, end)


def test_compute_missing_sessions_with_no_cache():
    sessions = _sessions(dt.datetime(2024, 1, 2), dt.datetime(2024, 1, 5))
    missing = helper.compute_missing_sessions(
        None, sessions, "minute", now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
    )
    assert missing == list(sessions.index)


def test_compute_missing_sessions_full_minute_cache_is_empty():
    sessions = _sessions(dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3))
    df = helper.parse_bars(make_minute_bars(dt.date(2024, 1, 3), 390), "minute")
    missing = helper.compute_missing_sessions(
        df, sessions, "minute", now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
    )
    assert missing == []


def test_compute_missing_sessions_detects_a_truncated_minute_session():
    """A response with only a handful of the session's minutes stays retryable."""
    sessions = _sessions(dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3))
    df = helper.parse_bars(make_minute_bars(dt.date(2024, 1, 3), 50), "minute")
    missing = helper.compute_missing_sessions(
        df, sessions, "minute", now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
    )
    assert missing == [dt.date(2024, 1, 3)]


def test_compute_missing_sessions_tolerates_a_few_absent_minutes():
    sessions = _sessions(dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3))
    df = helper.parse_bars(make_minute_bars(dt.date(2024, 1, 3), 380), "minute")
    missing = helper.compute_missing_sessions(
        df, sessions, "minute", now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
    )
    assert missing == []


def test_compute_missing_sessions_day_needs_only_one_bar():
    sessions = _sessions(dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 4))
    closes = helper._session_close_lookup(sessions)
    bar = FakeBar("20240103", 1.0, 2.0, 0.5, 1.5, 10)
    df = helper.parse_bars([bar], "day", session_closes=closes)
    missing = helper.compute_missing_sessions(
        df, sessions, "day", now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
    )
    assert missing == [dt.date(2024, 1, 4)]


def test_compute_missing_sessions_honours_placeholders():
    sessions = _sessions(dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3))
    placeholder = pd.DataFrame(
        {"missing": [True]}, index=pd.DatetimeIndex([pd.Timestamp("2024-01-03 21:00", tz="UTC")])
    )
    missing = helper.compute_missing_sessions(
        placeholder, sessions, "minute", now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
    )
    assert missing == []


def test_compute_missing_sessions_extended_hours_still_requires_rth_coverage():
    """use_rth=False must not become an escape hatch from truncation detection.

    An all-hours response still contains the regular session, so a handful of bars
    means the response was truncated, not that the session is complete.
    """
    sessions = _sessions(dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3))
    df = helper.parse_bars(make_minute_bars(dt.date(2024, 1, 3), 5), "minute")
    missing = helper.compute_missing_sessions(
        df, sessions, "minute", use_rth=False, now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
    )
    assert missing == [dt.date(2024, 1, 3)]

    full = helper.parse_bars(make_minute_bars(dt.date(2024, 1, 3), 390), "minute")
    assert (
        helper.compute_missing_sessions(
            full, sessions, "minute", use_rth=False, now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
        )
        == []
    )


def test_compute_missing_sessions_ignores_bars_outside_the_regular_session():
    """Pre/post-market bars must not be counted toward RTH coverage."""
    sessions = _sessions(dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3))
    rth = make_minute_bars(dt.date(2024, 1, 3), 390)
    premarket = [
        FakeBar("20240103  07:00:00", 1.0, 1.0, 1.0, 1.0, 1),
        FakeBar("20240103  08:00:00", 1.0, 1.0, 1.0, 1.0, 1),
    ]
    df = helper.parse_bars(premarket + rth[:5], "minute")
    missing = helper.compute_missing_sessions(
        df, sessions, "minute", use_rth=False, now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
    )
    assert missing == [dt.date(2024, 1, 3)]


# --------------------------------------------------------------------------------------
# End-to-end download behaviour
# --------------------------------------------------------------------------------------


def _run(asset, start, end, *, client, config, timespan="minute", now=None, **kwargs):
    return helper.get_price_data_from_ibkr_tws(
        asset,
        start,
        end,
        timespan=timespan,
        config=config,
        client=client,
        show_progress=False,
        now=now or dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
        **kwargs,
    )


def test_download_then_warm_cache_makes_zero_requests(cache_dir, config):
    asset = Asset("SPY")
    start, end = dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3)
    client = FakeClient(default=make_minute_bars(dt.date(2024, 1, 3), 390))

    df = _run(asset, start, end, client=client, config=config)
    assert len(df) == 390
    assert client.request_count == 1

    # Second call: same process, fresh client object -- must not be used at all.
    warm_client = FakeClient(default=[])
    df2 = _run(asset, start, end, client=warm_client, config=config)
    assert warm_client.request_count == 0
    assert len(df2) == 390

    # And with no client at all, the factory must never be invoked.
    factory = ExplodingClientFactory()
    df3 = helper.get_price_data_from_ibkr_tws(
        asset,
        start,
        end,
        timespan="minute",
        config=config,
        client_factory=factory,
        show_progress=False,
        now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
    )
    assert factory.calls == 0
    assert len(df3) == 390


def test_cache_file_and_metadata_are_written(cache_dir, config):
    asset = Asset("SPY")
    client = FakeClient(default=make_minute_bars(dt.date(2024, 1, 3), 390))
    _run(asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3), client=client, config=config)

    cache_file = helper.build_cache_filename(
        asset,
        "minute",
        None,
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
    )
    assert cache_file.exists()
    assert helper._meta_path(cache_file).exists()


def test_authoritative_empty_session_is_placeholdered_and_not_refetched(cache_dir, config):
    asset = Asset("SPY")
    start, end = dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3)
    client = FakeClient(default=[])

    df = _run(asset, start, end, client=client, config=config)
    assert df.empty
    assert client.request_count == 1

    second = FakeClient(default=[])
    df2 = _run(asset, start, end, client=second, config=config)
    assert second.request_count == 0
    assert df2.empty

    # The placeholder lives in the cache but never reaches the caller.
    cache_file = helper.build_cache_filename(
        asset, "minute", None, what_to_show=config.what_to_show, use_rth=config.use_rth
    )
    cached = helper.load_cache(cache_file)
    assert bool(cached["missing"].iloc[0]) is True
    assert "open" not in df2.columns or df2.empty


def test_failed_chunk_raises_and_is_not_negatively_cached(cache_dir, config):
    """A transport failure must surface loudly and stay retryable.

    Returning a short frame instead would let a backtest run on a silently
    incomplete history, which is far worse than a hard error.
    """
    asset = Asset("SPY")
    start, end = dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3)
    client = FakeClient(default=helper.IBKRTWSTimeoutError("boom"))

    with pytest.raises(helper.IBKRTWSTimeoutError):
        _run(asset, start, end, client=client, config=config)
    assert client.request_count == 1

    retry = FakeClient(default=make_minute_bars(dt.date(2024, 1, 3), 390))
    df2 = _run(asset, start, end, client=retry, config=config)
    assert retry.request_count == 1, "the failed session must be retried"
    assert len(df2) == 390


def test_truncated_response_leaves_sessions_retryable(cache_dir, config):
    """IB silently caps responses; the omitted sessions must not look cached."""
    asset = Asset("SPY")
    start, end = dt.datetime(2024, 1, 2), dt.datetime(2024, 1, 5)
    # Only the newest session comes back, as if the response hit the bar cap.
    client = FakeClient(default=make_minute_bars(dt.date(2024, 1, 5), 390))
    _run(asset, start, end, client=client, config=config)

    retry = FakeClient(default=make_minute_bars(dt.date(2024, 1, 4), 390))
    _run(asset, start, end, client=retry, config=config)
    assert retry.request_count >= 1, "truncated sessions must be retried"


def test_mixed_chunk_results_persist_progress_then_raise(cache_dir, config):
    """A mid-run failure keeps the chunks that succeeded but still raises."""
    asset = Asset("SPY")
    start, end = dt.datetime(2024, 1, 2), dt.datetime(2024, 2, 9)
    cfg = config.replace(minute_chunk_days=7)

    sessions = helper.get_trading_sessions(start, end)
    chunks = helper.build_chunks(list(sessions.index), "minute", 7)
    assert len(chunks) >= 3

    responses = {}
    for idx, (first, last) in enumerate(chunks):
        end_key = (
            pd.Timestamp(sessions["market_close"][last]).tz_convert(EASTERN) + dt.timedelta(minutes=1)
        ).date()
        if idx == 1:
            responses[end_key] = helper.IBKRTWSTimeoutError("chunk failed")
        else:
            bars = []
            for session_date in sessions.index:
                if first <= session_date <= last:
                    bars.extend(make_minute_bars(session_date, 390))
            responses[end_key] = bars

    client = FakeClient(responses=responses, default=[])
    with pytest.raises(helper.IBKRTWSTimeoutError):
        _run(asset, start, end, client=client, config=cfg)

    cache_file = helper.build_cache_filename(
        asset, "minute", None, what_to_show=cfg.what_to_show, use_rth=cfg.use_rth
    )
    cached = helper.load_cache(cache_file)
    cached_dates = {ts.date() for ts in cached.index}
    failed_first, failed_last = chunks[1]
    failed_sessions = [d for d in sessions.index if failed_first <= d <= failed_last]
    assert not (set(failed_sessions) & cached_dates), "failed chunk must not be cached at all"
    ok_first, ok_last = chunks[0]
    assert any(ok_first <= d <= ok_last for d in cached_dates)


def test_unfinished_session_is_never_placeholdered(cache_dir, config):
    """Today's still-open session must not be marked as authoritatively empty."""
    asset = Asset("SPY")
    start = dt.datetime(2024, 1, 3)
    end = dt.datetime(2024, 1, 3)
    now = EASTERN.localize(dt.datetime(2024, 1, 3, 11, 0)).astimezone(dt.timezone.utc)

    client = FakeClient(default=[])
    _run(asset, start, end, client=client, config=config, now=now)

    cache_file = helper.build_cache_filename(
        asset, "minute", None, what_to_show=config.what_to_show, use_rth=config.use_rth
    )
    cached = helper.load_cache(cache_file)
    assert cached is None or cached.empty


def test_contract_error_raises_and_writes_no_placeholder(cache_dir, config):
    asset = Asset("NOTREAL")
    client = FakeClient(default=helper.IBKRTWSContractError("IB error 200: No security definition"))
    with pytest.raises(helper.IBKRTWSContractError):
        _run(asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3), client=client, config=config)

    cache_file = helper.build_cache_filename(
        asset, "minute", None, what_to_show=config.what_to_show, use_rth=config.use_rth
    )
    assert not cache_file.exists()


def test_permission_error_raises_and_writes_no_placeholder(cache_dir, config):
    asset = Asset("SPY")
    client = FakeClient(default=helper.IBKRTWSPermissionError("IB error 354: not subscribed"))
    with pytest.raises(helper.IBKRTWSPermissionError):
        _run(asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3), client=client, config=config)
    cache_file = helper.build_cache_filename(
        asset, "minute", None, what_to_show=config.what_to_show, use_rth=config.use_rth
    )
    assert not cache_file.exists()


def test_only_missing_sessions_are_requested(cache_dir, config):
    asset = Asset("SPY")
    day_one = make_minute_bars(dt.date(2024, 1, 3), 390)
    client = FakeClient(default=day_one)
    _run(asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3), client=client, config=config)

    # Widen the window by one session; only the new session should be fetched.
    second = FakeClient(default=make_minute_bars(dt.date(2024, 1, 4), 390))
    _run(asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 4), client=second, config=config)
    assert second.request_count == 1
    requested_end = second.requests[0]["end"].astimezone(EASTERN).date()
    assert requested_end == dt.date(2024, 1, 4)


def test_day_timespan_end_to_end(cache_dir, config):
    asset = Asset("SPY")
    bars = [
        FakeBar("20241129", 1.0, 2.0, 0.5, 1.5, 10),
        FakeBar("20241202", 1.5, 2.5, 1.0, 2.0, 12),
    ]
    client = FakeClient(default=bars)
    df = _run(
        asset,
        dt.datetime(2024, 12, 2),
        dt.datetime(2024, 12, 2),
        client=client,
        config=config,
        timespan="day",
    )
    assert len(df) == 1
    assert [ts.tz_convert(EASTERN).hour for ts in df.index] == [16]
    assert client.requests[0]["bar_size"] == "1 day"
    cache_file = helper.build_cache_filename(
        asset,
        "day",
        None,
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
    )
    cached = helper.load_cache(cache_file)
    assert cached is not None
    assert {ts.tz_convert(EASTERN).date() for ts in cached.index} == {dt.date(2024, 12, 2)}


def test_force_cache_update_redownloads(cache_dir, config):
    asset = Asset("SPY")
    client = FakeClient(default=make_minute_bars(dt.date(2024, 1, 3), 390))
    _run(asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3), client=client, config=config)

    second = FakeClient(default=make_minute_bars(dt.date(2024, 1, 3), 390))
    helper.get_price_data_from_ibkr_tws(
        asset,
        dt.datetime(2024, 1, 3),
        dt.datetime(2024, 1, 3),
        timespan="minute",
        config=config,
        client=second,
        force_cache_update=True,
        show_progress=False,
        now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
    )
    assert second.request_count == 1


def test_force_cache_update_replaces_stale_rows_even_when_response_is_empty(cache_dir, config):
    asset = Asset("SPY")
    start = end = dt.datetime(2024, 1, 3)
    initial = FakeClient(default=make_minute_bars(dt.date(2024, 1, 3), 390))
    _run(asset, start, end, client=initial, config=config)

    refreshed = FakeClient(default=[])
    result = helper.get_price_data_from_ibkr_tws(
        asset,
        start,
        end,
        timespan="minute",
        config=config,
        client=refreshed,
        force_cache_update=True,
        show_progress=False,
        now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
    )
    assert result.empty
    assert refreshed.request_count == 1
    cache_file = helper.build_cache_filename(
        asset,
        "minute",
        None,
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
    )
    cached = helper.load_cache(cache_file)
    assert cached is not None
    assert helper._real_rows(cached).empty
    assert helper._missing_mask(cached).any()


def test_incomplete_force_refresh_preserves_cached_rows(cache_dir, config):
    asset = Asset("SPY")
    start, end = dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 4)
    original = make_minute_bars(dt.date(2024, 1, 3), 390) + make_minute_bars(
        dt.date(2024, 1, 4), 390
    )
    _run(asset, start, end, client=FakeClient(default=original), config=config)

    # A truncated response reaches only the newest requested session. The older
    # one remains unresolved, so the forced refresh must merge rather than replace.
    partial_client = FakeClient(default=make_minute_bars(dt.date(2024, 1, 4), 390))
    refreshed = helper.get_price_data_from_ibkr_tws(
        asset,
        start,
        end,
        timespan="minute",
        config=config,
        client=partial_client,
        force_cache_update=True,
        show_progress=False,
        now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
    )

    dates = set(refreshed.index.tz_convert(EASTERN).date)
    assert dates == {dt.date(2024, 1, 3), dt.date(2024, 1, 4)}
    assert len(refreshed) == 780


def test_failed_force_refresh_preserves_cached_rows(cache_dir, config):
    asset = Asset("SPY")
    start = end = dt.datetime(2024, 1, 3)
    original = make_minute_bars(dt.date(2024, 1, 3), 390)
    _run(asset, start, end, client=FakeClient(default=original), config=config)

    with pytest.raises(helper.IBKRTWSTimeoutError):
        helper.get_price_data_from_ibkr_tws(
            asset,
            start,
            end,
            timespan="minute",
            config=config,
            client=FakeClient(default=helper.IBKRTWSTimeoutError("request timed out")),
            force_cache_update=True,
            show_progress=False,
            now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
        )

    cache_file = helper.build_cache_filename(
        asset,
        "minute",
        None,
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
    )
    cached = helper.load_cache(cache_file)
    assert cached is not None
    assert len(helper._real_rows(cached)) == 390


def test_returned_frame_is_filtered_to_the_requested_range(cache_dir, config):
    asset = Asset("SPY")
    bars = make_minute_bars(dt.date(2024, 1, 3), 390) + make_minute_bars(dt.date(2024, 1, 4), 390)
    client = FakeClient(default=bars)
    _run(asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 4), client=client, config=config)

    narrowed = _run(
        asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3), client=FakeClient(default=[]), config=config
    )
    assert {ts.tz_convert(EASTERN).date() for ts in narrowed.index} == {dt.date(2024, 1, 3)}


def test_an_owned_client_is_closed_even_on_failure(cache_dir, config):
    created = []

    def factory(cfg):
        client = FakeClient(default=helper.IBKRTWSContractError("IB error 200"))
        created.append(client)
        return client

    with pytest.raises(helper.IBKRTWSContractError):
        helper.get_price_data_from_ibkr_tws(
            Asset("SPY"),
            dt.datetime(2024, 1, 3),
            dt.datetime(2024, 1, 3),
            timespan="minute",
            config=config,
            client_factory=factory,
            show_progress=False,
            now=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
        )
    assert created and created[0].closed is True


def test_an_injected_client_is_not_closed(cache_dir, config):
    client = FakeClient(default=make_minute_bars(dt.date(2024, 1, 3), 390))
    _run(Asset("SPY"), dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3), client=client, config=config)
    assert client.closed is False


# --------------------------------------------------------------------------------------
# Cache identity
# --------------------------------------------------------------------------------------


def test_cache_filenames_separate_request_shapes(cache_dir):
    asset = Asset("SPY")
    rth = helper.build_cache_filename(asset, "minute", use_rth=True)
    all_hours = helper.build_cache_filename(asset, "minute", use_rth=False)
    midpoint = helper.build_cache_filename(asset, "minute", what_to_show="MIDPOINT")
    eur = helper.build_cache_filename(asset, "minute", currency="EUR")
    island = helper.build_cache_filename(asset, "minute", exchange="ISLAND")
    day = helper.build_cache_filename(asset, "day")
    names = {rth, all_hours, midpoint, eur, island, day}
    assert len(names) == 6


def test_cache_metadata_mismatch_forces_a_rebuild(cache_dir, config):
    asset = Asset("SPY")
    client = FakeClient(default=make_minute_bars(dt.date(2024, 1, 3), 390))
    _run(asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3), client=client, config=config)

    cache_file = helper.build_cache_filename(
        asset, "minute", None, what_to_show=config.what_to_show, use_rth=config.use_rth
    )
    helper._write_meta(cache_file, {"schema_version": 999})

    second = FakeClient(default=make_minute_bars(dt.date(2024, 1, 3), 390))
    _run(asset, dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 3), client=second, config=config)
    assert second.request_count == 1


def test_update_cache_writes_atomically(tmp_path):
    cache_file = tmp_path / "x.parquet"
    df = helper.parse_bars(make_minute_bars(dt.date(2024, 1, 3), 3), "minute")
    helper.update_cache(cache_file, df, [], meta={"schema_version": 1})
    assert cache_file.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_load_cache_missing_file_returns_none(tmp_path):
    assert helper.load_cache(tmp_path / "nope.parquet") is None


def test_load_cache_corrupt_file_returns_none(tmp_path):
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"not parquet")
    assert helper.load_cache(bad) is None


# --------------------------------------------------------------------------------------
# Pacing
# --------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += max(seconds, 0.0)


def test_pacing_guard_enforces_the_ten_minute_window():
    clock = FakeClock()
    guard = helper.PacingGuard(3, time_func=clock.time, sleep_func=clock.sleep)
    for i in range(3):
        guard.acquire(("sig", i), "SPY")
        clock.now += 1.0
    assert clock.slept == []
    guard.acquire(("sig", 99), "SPY")
    assert clock.slept, "the 4th request in the window must wait"
    assert clock.slept[0] > 0


def test_pacing_guard_enforces_the_identical_request_cooldown():
    clock = FakeClock()
    guard = helper.PacingGuard(100, time_func=clock.time, sleep_func=clock.sleep)
    guard.acquire(("same",), "SPY")
    clock.now += 1.0
    guard.acquire(("same",), "SPY")
    assert clock.slept
    assert clock.slept[0] == pytest.approx(helper.IDENTICAL_REQUEST_COOLDOWN - 1.0)


def test_pacing_guard_enforces_the_same_contract_cooldown():
    clock = FakeClock()
    guard = helper.PacingGuard(100, time_func=clock.time, sleep_func=clock.sleep)
    guard.acquire(("a",), "SPY")
    guard.acquire(("b",), "SPY")
    assert clock.slept[0] == pytest.approx(helper.SAME_CONTRACT_COOLDOWN)


def test_pacing_guard_does_not_delay_different_contracts():
    clock = FakeClock()
    guard = helper.PacingGuard(100, time_func=clock.time, sleep_func=clock.sleep)
    guard.acquire(("a",), "SPY")
    guard.acquire(("b",), "QQQ")
    assert clock.slept == []


def test_pacing_guards_are_shared_per_gateway():
    a = helper.get_pacing_guard("127.0.0.1", 4002, 55)
    b = helper.get_pacing_guard("127.0.0.1", 4002, 55)
    c = helper.get_pacing_guard("127.0.0.1", 7497, 55)
    assert a is b
    assert a is not c


# --------------------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (200, "No security definition has been found", helper.IBKRTWSContractError),
        (354, "Requested market data is not subscribed", helper.IBKRTWSPermissionError),
        (502, "Couldn't connect to TWS", helper.IBKRTWSConnectionError),
        (504, "Not connected", helper.IBKRTWSConnectionError),
        (326, "client id is already in use", helper.IBKRTWSConnectionError),
        (162, "HMDS query returned no data", helper._NoDataSignal),
        (162, "Historical Market Data Service error message:Trading TWS session is connected from a different IP", helper.IBKRTWSError),
    ],
)
def test_classify_ib_error(code, message, expected):
    error = helper._classify_ib_error(code, message, FakeContract())
    assert isinstance(error, expected)
    assert str(code) in str(error)


def test_no_data_signal_is_an_ibkr_error_subclass():
    assert issubclass(helper._NoDataSignal, helper.IBKRTWSError)


@pytest.mark.parametrize(
    "args,expected_code,expected_message",
    [
        ((200, "No security definition"), 200, "No security definition"),
        ((200, "No security definition", ""), 200, "No security definition"),
        ((1700000000, 200, "No security definition", ""), 200, "No security definition"),
    ],
)
def test_parse_error_args_across_ibapi_versions(args, expected_code, expected_message):
    code, message = helper._parse_error_args(args)
    assert code == expected_code
    assert message == expected_message


def test_is_authoritative_empty():
    assert helper._is_authoritative_empty("HMDS query returned no data")
    assert helper._is_authoritative_empty("No historical market data for SPY/TRADES")
    assert not helper._is_authoritative_empty("pacing violation")


def test_format_end_datetime_variants():
    stamp = dt.datetime(2024, 1, 3, 21, 0, tzinfo=dt.timezone.utc)
    assert helper._format_end_datetime(stamp) == "20240103 21:00:00 UTC"
    assert helper._format_end_datetime(stamp, with_timezone=False) == "20240103 16:00:00"


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("INTERACTIVE_BROKERS_IP", "127.0.0.1")
    monkeypatch.setenv("INTERACTIVE_BROKERS_PORT", "7497")
    monkeypatch.setenv("IBKR_BACKTEST_CLIENT_ID", "91")
    monkeypatch.setenv("IBKR_BACKTEST_USE_RTH", "false")
    monkeypatch.setenv("IBKR_BACKTEST_WHAT_TO_SHOW", "midpoint")
    cfg = helper.IBKRTWSConfig.from_env()
    assert (cfg.host, cfg.port, cfg.client_id) == ("127.0.0.1", 7497, 91)
    assert cfg.use_rth is False
    assert cfg.what_to_show == "MIDPOINT"


def test_config_defaults_avoid_live_client_ids():
    cfg = helper.IBKRTWSConfig()
    assert cfg.client_id >= 12, "must not collide with live strategy client ids 1-11"
    assert cfg.use_rth is True
    assert cfg.what_to_show == "TRADES"


def test_config_from_env_ignores_invalid_values(monkeypatch):
    monkeypatch.setenv("INTERACTIVE_BROKERS_PORT", "not-a-port")
    cfg = helper.IBKRTWSConfig.from_env()
    assert cfg.port == helper.DEFAULT_PORT


def test_config_overrides_win_over_env(monkeypatch):
    monkeypatch.setenv("INTERACTIVE_BROKERS_PORT", "7497")
    cfg = helper.IBKRTWSConfig.from_env(port=4001, client_id=None)
    assert cfg.port == 4001
    assert cfg.client_id == helper.DEFAULT_CLIENT_ID


# --------------------------------------------------------------------------------------
# Cache-poisoning regressions (rubber-duck findings)
# --------------------------------------------------------------------------------------


def test_zero_padding_does_not_claim_older_sessions_as_authoritative():
    sessions = [dt.date(2024, 1, 3), dt.date(2024, 1, 4), dt.date(2024, 1, 5)]
    zero = [
        FakeBar(int(EASTERN.localize(dt.datetime.combine(day, dt.time(9, 30))).timestamp()), 0, 0, 0, 0, 0)
        for day in sessions[:2]
    ]
    real = make_minute_bars(sessions[1], 390)
    earliest = helper._earliest_raw_session(zero + real, "minute")
    assert earliest == sessions[1]
    result = helper.ChunkResult(
        helper.CHUNK_COMPLETE,
        sessions[0],
        sessions[2],
        bars=zero + real,
    )
    assert result.authoritative_sessions(sessions, earliest) == {sessions[2]}


def test_zero_only_raw_response_has_no_earliest_usable_session():
    zero = make_minute_bars(dt.date(2024, 1, 3), 2)
    for bar in zero:
        bar.open = bar.high = bar.low = bar.close = 0
    assert helper._earliest_raw_session(zero, "minute") is None


def test_all_zero_response_is_retried_on_the_next_run(cache_dir, config):
    """IB sometimes pads a response with all-zero OHLC rows.

    Those rows are dropped as fake data (RULE #1), which leaves the chunk looking
    empty. It must NOT be recorded as an authoritative "no data" session, or the
    range would never be retried.
    """
    asset = Asset("SPY")
    session = dt.date(2024, 1, 3)
    start = end = dt.datetime(2024, 1, 3)

    zero_bars = [
        FakeBar(int(stamp.astimezone(dt.timezone.utc).timestamp()), 0.0, 0.0, 0.0, 0.0, 0)
        for stamp in (
            EASTERN.localize(dt.datetime(2024, 1, 3, 9, 30)) + dt.timedelta(minutes=i)
            for i in range(390)
        )
    ]
    poisoned = FakeClient(default=zero_bars)
    df = _run(asset, start, end, client=poisoned, config=config)
    assert df.empty, "all-zero bars are not real prices and must be dropped"
    assert poisoned.request_count == 1

    # Next run must hit the network again and can then return the real bars.
    healthy = FakeClient(default=make_minute_bars(session, 390))
    df2 = _run(asset, start, end, client=healthy, config=config)
    assert healthy.request_count >= 1, "a zeroed-out response must not poison the cache"
    assert len(df2) == 390


def test_concurrent_writers_do_not_lose_each_others_sessions(cache_dir, config):
    """Two processes/threads writing the same cache file must merge, not clobber.

    ``update_cache`` re-reads the on-disk frame inside an interprocess lock, so a
    writer that started from a stale snapshot still preserves the other's rows.
    """
    asset = Asset("SPY")
    cache_file = helper.build_cache_filename(
        asset,
        "minute",
        None,
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
    )
    meta = helper._build_meta(
        asset,
        "minute",
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
        volume_multiplier=config.volume_multiplier,
    )

    frame_a = helper.parse_bars(make_minute_bars(dt.date(2024, 1, 3), 390), "minute")
    frame_b = helper.parse_bars(make_minute_bars(dt.date(2024, 1, 4), 390), "minute")

    errors = []

    def write(frame):
        try:
            for _ in range(5):
                helper.update_cache(cache_file, frame, meta=meta)
        except Exception as exc:  # pragma: no cover - surfaced via assert below
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(frame_a,)),
        threading.Thread(target=write, args=(frame_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    merged = helper.load_cache(cache_file)
    assert merged is not None
    sessions = {ts.tz_convert(EASTERN).date() for ts in merged.index}
    assert dt.date(2024, 1, 3) in sessions
    assert dt.date(2024, 1, 4) in sessions
    assert len(merged) == 780


def test_real_rows_replace_a_placeholder_for_the_same_session(cache_dir, config):
    """A later real download must evict the earlier "no data" placeholder."""
    asset = Asset("SPY")
    start = end = dt.datetime(2024, 1, 3)

    empty = FakeClient(default=[])
    df = _run(asset, start, end, client=empty, config=config)
    assert df.empty
    assert empty.request_count == 1

    cache_file = helper.build_cache_filename(
        asset,
        "minute",
        None,
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
    )
    cached = helper.load_cache(cache_file)
    assert cached is not None and not cached.empty, "a placeholder row should be recorded"

    meta = helper._build_meta(
        asset,
        "minute",
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
        volume_multiplier=config.volume_multiplier,
    )
    helper.update_cache(
        cache_file,
        helper.parse_bars(make_minute_bars(dt.date(2024, 1, 3), 390), "minute"),
        meta=meta,
    )

    merged = helper.load_cache(cache_file)
    assert helper._missing_mask(merged).sum() == 0, "placeholder must be dropped once real rows land"
    assert len(merged) == 390
