from datetime import timedelta

import pandas as pd
import pytest

from lumibot.tools import yahoo_helper
from lumibot.tools.yahoo_helper import YahooHelper


class _RecordingTicker:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response.copy()


def _frame(*timestamps):
    index = pd.DatetimeIndex(timestamps, tz="America/New_York")
    return pd.DataFrame({"Close": range(len(index))}, index=index)


@pytest.fixture
def fixed_now(monkeypatch):
    now = pd.Timestamp("2026-09-25 12:00:37", tz="America/New_York").to_pydatetime()
    monkeypatch.setattr(yahoo_helper, "get_lumibot_datetime", lambda: now)
    monkeypatch.setattr(YahooHelper, "sleep_and_get_proxy", staticmethod(lambda: None))
    return now.replace(second=0, microsecond=0)


@pytest.fixture(autouse=True)
def clear_invalid_symbols():
    yahoo_helper.INVALID_SYMBOLS.clear()
    yield
    yahoo_helper.INVALID_SYMBOLS.clear()


def test_minute_download_uses_yahoo_safe_windows(fixed_now):
    ticker = _RecordingTicker([pd.DataFrame()] * 5)

    result = YahooHelper._download_1m_chunked(ticker)

    assert result is None
    assert len(ticker.calls) == 5
    assert all(call["interval"] == "1m" for call in ticker.calls)
    assert all(call["auto_adjust"] is False for call in ticker.calls)
    assert all(call["end"] - call["start"] <= timedelta(days=7) for call in ticker.calls)
    assert all(call["start"] < call["end"] for call in ticker.calls)
    assert all(
        ticker.calls[index]["start"] == ticker.calls[index + 1]["end"]
        for index in range(len(ticker.calls) - 1)
    )
    assert ticker.calls[0]["end"] == fixed_now
    assert ticker.calls[-1]["start"] == fixed_now - timedelta(days=29)


def test_minute_download_sorts_and_deduplicates_boundaries(fixed_now):
    boundary = "2026-09-18 12:00:00"
    ticker = _RecordingTicker(
        [
            _frame(boundary, "2026-09-24 12:00:00"),
            _frame("2026-09-12 12:00:00", boundary),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
        ]
    )

    result = YahooHelper._download_1m_chunked(ticker)

    assert result is not None
    assert result.index.is_monotonic_increasing
    assert result.index.is_unique
    assert list(result.index.strftime("%Y-%m-%d")) == [
        "2026-09-12",
        "2026-09-18",
        "2026-09-24",
    ]


def test_minute_download_allows_empty_history_before_listing(fixed_now):
    ticker = _RecordingTicker(
        [
            _frame("2026-09-24 12:00:00"),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
        ]
    )

    result = YahooHelper._download_1m_chunked(ticker)

    assert result is not None
    assert list(result.index.strftime("%Y-%m-%d")) == ["2026-09-24"]


def test_minute_download_retries_failed_window_without_refetching_newer_data(fixed_now):
    ticker = _RecordingTicker(
        [
            _frame("2026-09-24 12:00:00"),
            RuntimeError("Yahoo unavailable"),
            RuntimeError("Yahoo unavailable"),
            _frame("2026-09-12 12:00:00"),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
        ]
    )

    result = YahooHelper._download_1m_chunked(ticker)

    assert result is not None
    assert len(ticker.calls) == 7
    assert list(result.index.strftime("%Y-%m-%d")) == ["2026-09-12", "2026-09-24"]


def test_minute_download_raises_after_window_retries_are_exhausted(fixed_now):
    ticker = _RecordingTicker([RuntimeError("Yahoo unavailable")] * 3)

    with pytest.raises(yahoo_helper.YahooMinuteChunkDownloadError, match="failed after 3 attempts"):
        YahooHelper._download_1m_chunked(ticker)


def test_minute_download_rejects_gap_between_populated_windows(fixed_now):
    ticker = _RecordingTicker(
        [
            _frame("2026-09-24 12:00:00"),
            pd.DataFrame(),
            _frame("2026-09-10 12:00:00"),
            pd.DataFrame(),
            pd.DataFrame(),
        ]
    )

    with pytest.raises(yahoo_helper.YahooMinuteChunkDownloadError, match="gap"):
        YahooHelper._download_1m_chunked(ticker)


def test_minute_download_rejects_inconsistent_columns(fixed_now):
    first = _frame("2026-09-24 12:00:00")
    second = _frame("2026-09-12 12:00:00").assign(Unexpected=1)
    ticker = _RecordingTicker(
        [first, second, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()]
    )

    with pytest.raises(yahoo_helper.YahooMinuteChunkDownloadError, match="inconsistent columns"):
        YahooHelper._download_1m_chunked(ticker)


@pytest.mark.parametrize(
    ("total_days", "chunk_days"),
    [(0, 7), (29, 0), (-1, 7), (29, -1)],
)
def test_minute_download_rejects_invalid_windows(fixed_now, total_days, chunk_days):
    ticker = _RecordingTicker([])

    with pytest.raises(ValueError, match="must be positive"):
        YahooHelper._download_1m_chunked(
            ticker,
            total_days=total_days,
            chunk_days=chunk_days,
        )


def test_minute_download_rejects_invalid_retry_count(fixed_now):
    ticker = _RecordingTicker([])

    with pytest.raises(ValueError, match="must be positive"):
        YahooHelper._download_1m_chunked(ticker, max_retries=0)


class _FakeYFinance:
    def __init__(self, ticker):
        self.ticker = ticker

    def Ticker(self, symbol):
        return self.ticker


def test_download_symbol_data_uses_chunked_minute_history(monkeypatch, fixed_now):
    ticker = _RecordingTicker(
        [
            _frame("2026-09-24 12:00:00"),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
        ]
    )
    monkeypatch.setattr(yahoo_helper, "yf", _FakeYFinance(ticker))
    monkeypatch.setattr(YahooHelper, "get_symbol_info", staticmethod(lambda symbol: None))

    result = YahooHelper.download_symbol_data("AAPL", interval="1m")

    assert result is not None
    assert len(ticker.calls) == 5
    assert result.index.is_unique


def test_chunk_failure_does_not_mark_symbol_invalid(monkeypatch, fixed_now):
    ticker = _RecordingTicker([RuntimeError("Yahoo unavailable")] * 3)
    monkeypatch.setattr(yahoo_helper, "yf", _FakeYFinance(ticker))

    result = YahooHelper.download_symbol_data("AAPL", interval="1m")

    assert result is None
    assert "AAPL" not in yahoo_helper.INVALID_SYMBOLS
