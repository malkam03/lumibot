"""Historical bar downloads straight from an IB Gateway / TWS socket API.

This module talks to the Interactive Brokers *socket* API (officially the "TWS
API", served by both Trader Workstation and IB Gateway) using
``ibapi.EClient.reqHistoricalData``. It deliberately does **not** depend on the
hosted Data Downloader HTTP service used by :mod:`lumibot.tools.ibkr_helper`.

Design notes
------------
* The public entry point :func:`get_price_data_from_ibkr_tws` mirrors
  :func:`lumibot.tools.polygon_helper.get_price_data_from_polygon`: parquet cache
  under ``LUMIBOT_CACHE_FOLDER/ibkr_tws``, only missing ranges are downloaded,
  and a fully warm cache performs **zero** network calls (no socket is even
  constructed).
* Coverage is tracked per *trading session*, not merely "some row exists on that
  date". IB responses are anchored backwards from ``endDateTime`` and are capped
  (a "1 M" request of 1-minute bars returns exactly 8190 bars), so a truncated
  response must not be mistaken for a complete one.
* Negative cache markers (``missing=True`` rows) are only written for sessions
  that an *authoritative, successful* request reported as empty. Failed,
  timed-out or truncated requests leave their sessions missing so they are
  retried. This keeps LumiBot's RULE #1 intact: absent data stays absent, it is
  never synthesised.
* Daily bars are stamped at the real NYSE session close (so early closes land at
  13:00 ET) rather than at midnight, which would let a daily backtest see the
  current session's OHLC at the open.

Scope of v1: stocks/ETFs (``secType=STK``) with ``minute`` and ``day`` bars.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import pandas_market_calendars as mcal
import pytz
from tqdm import tqdm

from lumibot.constants import LUMIBOT_CACHE_FOLDER
from lumibot.entities import Asset
from lumibot.tools.lumibot_logger import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

CACHE_SUBFOLDER = "ibkr_tws"
CACHE_SCHEMA_VERSION = 1

#: Map a LumiBot timespan onto an IB ``barSizeSetting``.
TIMESPAN_TO_BAR_SIZE = {
    "minute": "1 min",
    "day": "1 day",
}

#: Asset types supported by this data source in v1.
SUPPORTED_ASSET_TYPES = (Asset.AssetType.STOCK,)

#: A minute session counts as "covered" once this fraction of its expected RTH
#: minutes is present. IB occasionally omits a handful of zero-volume minutes,
#: so requiring 100% would cause endless re-downloads.
MIN_MINUTE_COVERAGE_RATIO = 0.90

#: Calendar-day span of a single historical request.
#:
#: IB caps a "1 M" minute request at 8190 bars, which is exactly 21 sessions x
#: 390 RTH minutes -- a 22-session month would truncate silently. 14 calendar
#: days is ~10 sessions (~3900 bars), comfortably under the cap.
DEFAULT_MINUTE_CHUNK_DAYS = 14
DEFAULT_DAY_CHUNK_DAYS = 365

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4002  # IB Gateway paper. 4001 = GW live, 7497 = TWS paper, 7496 = TWS live.
DEFAULT_CLIENT_ID = 77  # Well above the live strategy client ids (1-11).
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_REQUESTS_PER_10MIN = 55  # IB's hard limit is 60.
# Hard ceiling: a configured value above this would guarantee pacing violations,
# so IBKR_BACKTEST_MAX_REQUESTS_PER_10MIN is clamped rather than trusted.
MAX_REQUESTS_PER_10MIN_CEILING = 55

#: Minimum seconds between two *identical* historical requests (IB rule).
IDENTICAL_REQUEST_COOLDOWN = 15.0
#: Minimum seconds between requests for the same contract. IB rejects 6+
#: identical-contract requests inside 2 seconds.
SAME_CONTRACT_COOLDOWN = 0.35
#: Sliding pacing window, in seconds.
PACING_WINDOW = 600.0

_EASTERN = pytz.timezone("America/New_York")

# --- IB error code classification -------------------------------------------------------

#: Purely informational codes that must not fail a request.
BENIGN_ERROR_CODES = frozenset(
    {
        165,  # Historical data service informational message
        300,  # Can't find EId (cancel of an already finished request)
        399,  # Order message / warning
        2100,  # API client has been unsubscribed from account data
        2104,  # Market data farm connection is OK
        2106,  # HMDS data farm connection is OK
        2107,  # HMDS data farm connection is inactive but should be available on demand
        2108,  # Market data farm connection is inactive but should be available on demand
        2119,  # Market data farm is connecting
        2158,  # Sec-def data farm connection is OK
    }
)

#: Data-farm/connectivity degradation. Surfaced as warnings; an in-flight request
#: is failed (never silently treated as "no data").
FARM_DEGRADED_ERROR_CODES = frozenset(
    {
        1100,  # Connectivity between IB and TWS has been lost
        2103,  # Market data farm connection is broken
        2105,  # HMDS data farm connection is broken
        2157,  # Sec-def data farm connection is broken
    }
)

#: Connectivity restored notices.
CONNECTIVITY_RESTORED_ERROR_CODES = frozenset({1101, 1102})

#: Contract resolution problems. These are configuration errors, not "no data".
CONTRACT_ERROR_CODES = frozenset({200, 203, 321})

#: Entitlement / permission problems.
PERMISSION_ERROR_CODES = frozenset({354, 10089, 10090, 10197})

#: Fatal connection problems.
CONNECTION_ERROR_CODES = frozenset({326, 502, 504, 1300})

#: Invalid ``endDateTime`` (usually the timezone suffix).
INVALID_END_DATETIME_ERROR_CODE = 10314

HISTORICAL_SERVICE_ERROR_CODE = 162


# --------------------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------------------


class IBKRTWSError(Exception):
    """Base error for the IB Gateway / TWS socket backtesting data source."""


class IBKRTWSConnectionError(IBKRTWSError):
    """Raised when the gateway cannot be reached or drops the connection."""


class IBKRTWSTimeoutError(IBKRTWSError):
    """Raised when a historical data request does not complete in time."""


class IBKRTWSContractError(IBKRTWSError):
    """Raised when IB cannot resolve the requested contract (e.g. error 200)."""


class IBKRTWSPermissionError(IBKRTWSError):
    """Raised when the account lacks the market data subscription for a request."""


class IBKRTWSPacingViolation(IBKRTWSError):
    """Internal: IB reported a pacing violation (error 162). Retried with backoff."""


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class IBKRTWSConfig:
    """Connection + request settings for the IB Gateway / TWS socket API."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    client_id: int = DEFAULT_CLIENT_ID
    what_to_show: str = "TRADES"
    use_rth: bool = True
    timeout: float = DEFAULT_TIMEOUT
    max_requests_per_10min: int = DEFAULT_MAX_REQUESTS_PER_10MIN
    minute_chunk_days: int = DEFAULT_MINUTE_CHUNK_DAYS
    day_chunk_days: int = DEFAULT_DAY_CHUNK_DAYS
    exchange: str = "SMART"
    primary_exchange: str = ""
    currency: str = "USD"
    volume_multiplier: Optional[float] = None

    @classmethod
    def from_env(cls, **overrides: Any) -> "IBKRTWSConfig":
        """Build a config from the environment, then apply non-``None`` overrides."""
        base = cls(
            host=os.environ.get("INTERACTIVE_BROKERS_IP") or DEFAULT_HOST,
            port=_env_int("INTERACTIVE_BROKERS_PORT", DEFAULT_PORT),
            client_id=_env_int("IBKR_BACKTEST_CLIENT_ID", DEFAULT_CLIENT_ID),
            what_to_show=(os.environ.get("IBKR_BACKTEST_WHAT_TO_SHOW") or "TRADES").strip().upper(),
            use_rth=_env_bool("IBKR_BACKTEST_USE_RTH", True),
            timeout=_env_float("IBKR_BACKTEST_TIMEOUT", DEFAULT_TIMEOUT),
            max_requests_per_10min=_env_int(
                "IBKR_BACKTEST_MAX_REQUESTS_PER_10MIN", DEFAULT_MAX_REQUESTS_PER_10MIN
            ),
        )
        clean = {k: v for k, v in overrides.items() if v is not None}
        if not clean:
            return base
        return base.replace(**clean)

    def replace(self, **changes: Any) -> "IBKRTWSConfig":
        from dataclasses import replace as _replace

        return _replace(self, **changes)

    def effective_volume_multiplier(self, asset: Asset) -> float:
        """Return an explicit volume override, otherwise preserve IB API units."""
        if self.volume_multiplier is not None:
            return float(self.volume_multiplier)
        return 1.0


# --------------------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------------------


def _asset_type_str(asset: Asset) -> str:
    raw = getattr(asset, "asset_type", None)
    value = getattr(raw, "value", raw)
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.split(".")[-1]
    return text


def validate_asset(asset: Asset) -> None:
    """Raise :class:`NotImplementedError` for asset types outside v1 scope."""
    asset_type = _asset_type_str(asset)
    supported = {str(getattr(t, "value", t)).lower() for t in SUPPORTED_ASSET_TYPES}
    if asset_type not in supported:
        raise NotImplementedError(
            f"The IB Gateway/TWS backtesting data source does not support asset type "
            f"'{asset_type}' yet (asset={getattr(asset, 'symbol', asset)!r}). "
            f"Supported types: {sorted(supported)}. Options, futures, forex and crypto "
            "are out of scope for this data source; use another backtesting data source "
            "for those."
        )


def validate_quote_asset(quote_asset: Optional[Asset], config: "IBKRTWSConfig") -> None:
    """Reject a quote currency the IB contract would not actually be priced in.

    The contract currency comes from ``config.currency``; silently accepting a
    different quote would label USD prices as, say, EUR.
    """
    if quote_asset is None:
        return
    symbol = str(getattr(quote_asset, "symbol", "") or "").strip().upper()
    if not symbol or symbol == str(config.currency).strip().upper():
        return
    raise NotImplementedError(
        f"The IB Gateway/TWS backtesting data source prices contracts in "
        f"{config.currency}, but a quote of {symbol} was requested for "
        f"{getattr(quote_asset, 'symbol', quote_asset)!r}. Non-{config.currency} quotes "
        "are out of scope for this data source."
    )


def validate_timespan(timespan: str) -> str:
    """Normalise and validate a LumiBot timespan, returning the IB bar size."""
    key = str(timespan or "").strip().lower()
    if key not in TIMESPAN_TO_BAR_SIZE:
        raise ValueError(
            f"Unsupported timestep '{timespan}' for the IB Gateway/TWS backtesting data "
            f"source. Supported timesteps: {sorted(TIMESPAN_TO_BAR_SIZE)}."
        )
    return TIMESPAN_TO_BAR_SIZE[key]


# --------------------------------------------------------------------------------------
# Trading calendar
# --------------------------------------------------------------------------------------

_schedule_cache: Dict[Tuple[str, date, date], pd.DataFrame] = {}
_buffered_schedules: Dict[str, pd.DataFrame] = {}
_schedule_lock = threading.Lock()


def get_trading_sessions(start: datetime, end: datetime, calendar_name: str = "NYSE") -> pd.DataFrame:
    """Return the exchange sessions covering ``[start, end]``.

    Returns a DataFrame indexed by :class:`datetime.date` with tz-aware UTC
    ``market_open`` / ``market_close`` columns. Schedules are cached (and fetched
    with a forward buffer) because ``pandas_market_calendars`` lookups are slow.
    """
    start_date = start.date() if isinstance(start, datetime) else start
    end_date = end.date() if isinstance(end, datetime) else end
    cache_key = (calendar_name, start_date, end_date)

    with _schedule_lock:
        cached = _schedule_cache.get(cache_key)
        if cached is not None:
            return cached

        buffered = _buffered_schedules.get(calendar_name)
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        if (
            buffered is None
            or buffered.empty
            or buffered.index.min() > start_ts
            or buffered.index.max() < end_ts
        ):
            cal = mcal.get_calendar(calendar_name)
            buffered = cal.schedule(start_date=start_date, end_date=end_date + timedelta(days=30))
            _buffered_schedules[calendar_name] = buffered

        window = buffered[(buffered.index >= start_ts) & (buffered.index <= end_ts)]
        sessions = pd.DataFrame(
            {
                "market_open": pd.to_datetime(window["market_open"]).dt.tz_convert("UTC"),
                "market_close": pd.to_datetime(window["market_close"]).dt.tz_convert("UTC"),
            }
        )
        sessions.index = pd.Index([ts.date() for ts in window.index], name="session_date")
        _schedule_cache[cache_key] = sessions
        return sessions


def _session_close_lookup(sessions: pd.DataFrame) -> Dict[date, pd.Timestamp]:
    if sessions is None or sessions.empty:
        return {}
    return {d: ts for d, ts in zip(sessions.index, sessions["market_close"])}


def _expected_minutes(open_ts: pd.Timestamp, close_ts: pd.Timestamp) -> int:
    delta = (close_ts - open_ts).total_seconds() / 60.0
    return max(int(round(delta)), 1)


# --------------------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------------------

_cache_locks: Dict[str, threading.Lock] = {}
_cache_locks_guard = threading.Lock()


@contextlib.contextmanager
def _interprocess_cache_lock(cache_file: Path):
    """Advisory lock around a cache file, shared across processes.

    Several LumiBot processes routinely warm the same cache (parallel backtest
    workers). Without this, two processes can each read the old cache, download
    independently, and then overwrite one another - in the worst case replacing
    real bars with another process' "no data" placeholders.

    ``fcntl`` is POSIX-only; on platforms without it the in-process lock plus the
    atomic replace is the best available guarantee.
    """
    lock_path = cache_file.with_name(cache_file.name + ".lock")
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _combine_cached(*frames: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Merge cache frames so real bars always beat placeholders.

    Later frames win for the same timestamp, but a placeholder never survives for
    a session that has real bars anywhere in the merged result. That is what makes
    a concurrent "no data" writer unable to erase another writer's real data.
    """
    normalized = [_normalize_index(f) for f in frames if f is not None]
    normalized = [f for f in normalized if not f.empty]
    if not normalized:
        return pd.DataFrame()

    combined = pd.concat(normalized)
    real = _real_rows(combined)
    real = real[~real.index.duplicated(keep="last")].sort_index()
    real_dates = {ts.tz_convert(_EASTERN).date() for ts in real.index}

    if "missing" not in combined.columns:
        return real

    placeholders = combined[_missing_mask(combined)]
    placeholders = placeholders[~placeholders.index.duplicated(keep="first")]
    if not placeholders.empty:
        keep = [ts.tz_convert(_EASTERN).date() not in real_dates for ts in placeholders.index]
        placeholders = placeholders[keep]
    if placeholders.empty:
        return real
    out = pd.concat([real, placeholders]).sort_index()
    return out[~out.index.duplicated(keep="first")]


def _cache_lock_for(cache_file: Path) -> threading.Lock:
    key = str(cache_file)
    with _cache_locks_guard:
        lock = _cache_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _cache_locks[key] = lock
        return lock


def _sanitize(text: Any) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-.") else "_" for ch in str(text or ""))
    return cleaned.strip("_") or "NA"


def build_cache_filename(
    asset: Asset,
    timespan: str,
    quote_asset: Optional[Asset] = None,
    *,
    what_to_show: str = "TRADES",
    use_rth: bool = True,
    exchange: str = "SMART",
    currency: str = "USD",
    primary_exchange: str = "",
) -> Path:
    """Path of the parquet cache for one (contract, timespan, request-shape).

    ``primary_exchange`` is part of the identity because it disambiguates IB
    contract resolution for symbols listed on several venues; without it two
    different instruments could share one cache file.
    """
    folder = Path(LUMIBOT_CACHE_FOLDER) / CACHE_SUBFOLDER
    symbol = _sanitize(getattr(asset, "symbol", asset))
    if quote_asset is not None and getattr(quote_asset, "symbol", None):
        symbol = f"{symbol}_{_sanitize(quote_asset.symbol)}"
    parts = [
        _sanitize(_asset_type_str(asset)),
        symbol,
        _sanitize(currency),
        _sanitize(exchange),
        _sanitize(primary_exchange) if primary_exchange else "any",
        _sanitize(timespan),
        _sanitize(str(what_to_show).lower()),
        "rth" if use_rth else "all",
    ]
    return folder / ("_".join(parts) + ".parquet")


def _meta_path(cache_file: Path) -> Path:
    return cache_file.with_suffix(".meta.json")


def _build_meta(
    asset: Asset,
    timespan: str,
    *,
    what_to_show: str,
    use_rth: bool,
    exchange: str,
    currency: str,
    volume_multiplier: float,
    primary_exchange: str = "",
) -> dict:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "symbol": str(getattr(asset, "symbol", asset)),
        "asset_type": _asset_type_str(asset),
        "timespan": str(timespan),
        "what_to_show": str(what_to_show).upper(),
        "use_rth": bool(use_rth),
        "exchange": str(exchange),
        "primary_exchange": str(primary_exchange or ""),
        "currency": str(currency),
        "volume_multiplier": float(volume_multiplier),
    }


def _meta_matches(cache_file: Path, expected: dict) -> bool:
    path = _meta_path(cache_file)
    if not path.exists():
        # Legacy/partial cache without metadata: treat as incompatible so it is rebuilt.
        return False
    try:
        stored = json.loads(path.read_text())
    except Exception as exc:  # pragma: no cover - corrupt metadata is rare
        logger.warning("Unreadable IBKR TWS cache metadata at %s: %s", path, exc)
        return False
    return all(stored.get(key) == value for key, value in expected.items())


def _write_meta(cache_file: Path, meta: dict) -> None:
    path = _meta_path(cache_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True))
    os.replace(tmp, path)


def load_cache(cache_file: Path) -> Optional[pd.DataFrame]:
    """Load a parquet cache into a UTC-indexed DataFrame (``None`` when absent)."""
    cache_file = Path(str(cache_file))
    if not cache_file.exists():
        return None
    try:
        df = pd.read_parquet(cache_file, engine="pyarrow")
    except Exception as exc:
        logger.warning("Could not read IBKR TWS cache %s (%s); rebuilding it.", cache_file, exc)
        return None
    if df.empty:
        return None
    if "datetime" not in df.columns:
        logger.warning("IBKR TWS cache %s has no 'datetime' column; rebuilding it.", cache_file)
        return None
    df = df.set_index("datetime")
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.index.name = "datetime"
    return df.sort_index()


def update_cache(
    cache_file: Path,
    df_all: Optional[pd.DataFrame],
    placeholder_sessions: Optional[Sequence[date]] = None,
    *,
    session_closes: Optional[Dict[date, pd.Timestamp]] = None,
    meta: Optional[dict] = None,
    replace_existing: bool = False,
) -> pd.DataFrame:
    """Merge placeholder rows for authoritatively-empty sessions and persist.

    Placeholder rows carry only ``missing=True`` -- never synthetic OHLC -- and
    are filtered out before any data is handed back to a strategy.
    """
    if df_all is None:
        df_all = pd.DataFrame()
    df_all = _normalize_index(df_all)

    existing_dates = {ts.date() for ts in df_all.index} if not df_all.empty else set()
    session_closes = session_closes or {}

    rows = []
    for session_date in placeholder_sessions or []:
        if session_date in existing_dates:
            continue
        close_ts = session_closes.get(session_date)
        if close_ts is None:
            stamp = _EASTERN.localize(
                datetime(session_date.year, session_date.month, session_date.day, 16, 0)
            ).astimezone(timezone.utc)
        else:
            stamp = pd.Timestamp(close_ts).tz_convert("UTC").to_pydatetime()
        rows.append(stamp)

    if rows:
        placeholder_df = pd.DataFrame({"missing": [True] * len(rows)}, index=pd.DatetimeIndex(rows))
        placeholder_df.index.name = "datetime"
        df_all = pd.concat([df_all, placeholder_df]).sort_index()
        df_all = df_all[~df_all.index.duplicated(keep="first")]

    if df_all.empty and not replace_existing:
        return df_all

    lock = _cache_lock_for(cache_file)
    with lock:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with _interprocess_cache_lock(cache_file):
            if replace_existing and df_all.empty:
                cache_file.unlink(missing_ok=True)
                _meta_path(cache_file).unlink(missing_ok=True)
                return df_all
            # Re-read inside the lock: another writer may have added rows since this
            # caller loaded the cache, and a blind overwrite would drop them.
            on_disk = (
                load_cache(cache_file)
                if not replace_existing and (meta is None or _meta_matches(cache_file, meta))
                else None
            )
            df_all = _combine_cached(on_disk, df_all)
            if df_all.empty:
                return df_all
            tmp = cache_file.with_name(cache_file.name + f".{os.getpid()}.tmp")
            df_all.reset_index().to_parquet(tmp, engine="pyarrow", compression="snappy")
            os.replace(tmp, cache_file)
            if meta is not None:
                _write_meta(cache_file, meta)
    return df_all


def _normalize_index(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.index.name = "datetime"
    return df.sort_index()


def _missing_mask(df: pd.DataFrame) -> pd.Series:
    """Boolean mask of placeholder rows.

    The ``missing`` column can be object dtype after a concat/parquet round trip,
    so coerce explicitly instead of relying on ``fillna`` downcasting.
    """
    column = df["missing"]
    return pd.Series(
        [bool(v) if pd.notna(v) else False for v in column],
        index=df.index,
        dtype=bool,
    )


def _real_rows(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if "missing" not in df.columns:
        return df
    return df[~_missing_mask(df)]


def _placeholder_dates(df: Optional[pd.DataFrame]) -> set:
    if df is None or df.empty or "missing" not in df.columns:
        return set()
    return {ts.date() for ts in df.index[_missing_mask(df)]}


# --------------------------------------------------------------------------------------
# Missing-session computation
# --------------------------------------------------------------------------------------


def compute_missing_sessions(
    df_all: Optional[pd.DataFrame],
    sessions: pd.DataFrame,
    timespan: str,
    *,
    use_rth: bool = True,
    now: Optional[datetime] = None,
    min_minute_coverage_ratio: float = MIN_MINUTE_COVERAGE_RATIO,
) -> List[date]:
    """Sessions in ``sessions`` that are not adequately covered by ``df_all``.

    A session is covered when it has an authoritative placeholder, or enough real
    bars. For minute data "enough" means at least
    ``min_minute_coverage_ratio`` of the session's expected RTH minutes, which is
    what catches IB responses silently truncated at the bar cap. In-progress
    sessions remain missing regardless of current coverage and are never
    placeholdered (see :func:`get_price_data_from_ibkr_tws`).
    """
    if sessions is None or sessions.empty:
        return []

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    placeholders = _placeholder_dates(df_all)
    real = _real_rows(df_all)

    counts: Dict[date, int] = {}
    if not real.empty:
        if timespan == "day":
            # Day bars are stamped at the session close, so the Eastern calendar
            # date of the timestamp is the session.
            index = [ts.tz_convert(_EASTERN).date() for ts in real.index]
        else:
            # Count only bars that fall inside the session's regular hours. An
            # extended-hours request returns pre/post bars too, and a bar at
            # 20:00 ET lands on the *next* UTC date, so neither a raw UTC date
            # nor a raw count is a usable coverage measure.
            index = []
            windows = {
                d: (row["market_open"], row["market_close"]) for d, row in sessions.iterrows()
            }
            for ts in real.index:
                eastern = ts.tz_convert(_EASTERN)
                window = windows.get(eastern.date())
                if window is None:
                    continue
                if window[0] <= ts < window[1]:
                    index.append(eastern.date())
        if index:
            counts = pd.Series(1, index=pd.Index(index)).groupby(level=0).sum().to_dict()

    missing: List[date] = []
    for session_date, row in sessions.iterrows():
        if not _session_is_closed(row["market_close"], session_date, now_utc):
            missing.append(session_date)
            continue
        if session_date in placeholders:
            continue
        have = int(counts.get(session_date, 0))
        if timespan == "day":
            if have >= 1:
                continue
        else:
            # The RTH coverage threshold is applied even when extended hours were
            # requested: an all-hours response still contains the RTH window, so a
            # session that is short on RTH minutes was truncated either way.
            expected = _expected_minutes(row["market_open"], row["market_close"])
            if have >= math.ceil(expected * min_minute_coverage_ratio):
                continue
        missing.append(session_date)
    return missing


def build_chunks(missing_sessions: Sequence[date], timespan: str, chunk_days: int) -> List[Tuple[date, date]]:
    """Group missing sessions into contiguous ``(first_date, last_date)`` windows.

    Each window spans at most ``chunk_days`` calendar days so a single IB request
    stays well under the per-response bar cap.
    """
    if not missing_sessions:
        return []
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")

    ordered = sorted(set(missing_sessions))
    chunks: List[Tuple[date, date]] = []
    window_start = ordered[0]
    window_end = ordered[0]
    for current in ordered[1:]:
        if (current - window_start).days + 1 <= chunk_days:
            window_end = current
        else:
            chunks.append((window_start, window_end))
            window_start = current
            window_end = current
    chunks.append((window_start, window_end))
    return chunks


def format_duration(first: date, last: date) -> str:
    """IB ``durationStr`` covering ``[first, last]`` inclusive."""
    days = (last - first).days + 1
    # +1 day of slack: IB counts back from endDateTime, and the end anchor sits at
    # the session close rather than midnight. Remove that one slack day before
    # converting to years so a maximum 365-calendar-day chunk stays at "1 Y".
    days += 1
    if days <= 365:
        return f"{days} D"
    years = max(1, math.ceil((days - 1) / 365))
    return f"{years} Y"


# --------------------------------------------------------------------------------------
# Bar parsing
# --------------------------------------------------------------------------------------


def _bar_value(bar: Any, name: str) -> Any:
    if isinstance(bar, dict):
        return bar.get(name)
    return getattr(bar, name, None)


def parse_ib_datetime(
    value: Any,
    timespan: str,
    session_closes: Optional[Dict[date, pd.Timestamp]] = None,
) -> Optional[pd.Timestamp]:
    """Convert an IB bar timestamp into a UTC :class:`pandas.Timestamp`.

    Handles every format TWS emits: epoch seconds (``formatDate=2``),
    ``"YYYYMMDD"`` (daily bars), and ``"YYYYMMDD  HH:MM:SS"`` with an optional
    trailing timezone name. Naive intraday strings are interpreted as
    America/New_York. Daily bars are re-stamped at the real session close so a
    daily backtest cannot see the current session at the open.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = pd.Timestamp(int(value), unit="s", tz="UTC")
        return _finalize_timestamp(ts, timespan, session_closes)

    text = str(value).strip()
    if not text:
        return None

    parts = text.split()
    tz_name: Optional[str] = None
    if len(parts) == 3:
        tz_name = parts[2]
        text = f"{parts[0]} {parts[1]}"
        parts = parts[:2]

    if len(parts) == 1 and parts[0].isdigit():
        token = parts[0]
        if len(token) == 8:  # YYYYMMDD
            ts = pd.Timestamp(datetime.strptime(token, "%Y%m%d"), tz=_EASTERN).tz_convert("UTC")
            return _finalize_timestamp(ts, timespan, session_closes)
        # Epoch seconds (10 digits today, more defensive for longer tokens).
        ts = pd.Timestamp(int(token), unit="s", tz="UTC")
        return _finalize_timestamp(ts, timespan, session_closes)

    naive = pd.Timestamp(datetime.strptime(text, "%Y%m%d %H:%M:%S"))
    tz = _EASTERN
    if tz_name:
        try:
            tz = pytz.timezone(tz_name)
        except Exception:
            if tz_name.upper() in {"UTC", "GMT"}:
                tz = pytz.UTC
            else:
                logger.debug("Unknown IB timezone suffix %r; assuming America/New_York", tz_name)
    ts = naive.tz_localize(tz).tz_convert("UTC")
    return _finalize_timestamp(ts, timespan, session_closes)


def _finalize_timestamp(
    ts: pd.Timestamp,
    timespan: str,
    session_closes: Optional[Dict[date, pd.Timestamp]],
) -> pd.Timestamp:
    if timespan != "day":
        return ts
    session_date = ts.tz_convert(_EASTERN).date()
    close_ts = (session_closes or {}).get(session_date)
    if close_ts is not None:
        return pd.Timestamp(close_ts).tz_convert("UTC")
    fallback = _EASTERN.localize(
        datetime(session_date.year, session_date.month, session_date.day, 16, 0)
    )
    return pd.Timestamp(fallback).tz_convert("UTC")


def parse_bars(
    bars: Sequence[Any],
    timespan: str,
    *,
    session_closes: Optional[Dict[date, pd.Timestamp]] = None,
    volume_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Convert raw IB ``BarData`` objects into LumiBot's OHLCV frame format."""
    records = []
    for bar in bars or []:
        stamp = parse_ib_datetime(_bar_value(bar, "date"), timespan, session_closes)
        if stamp is None:
            continue
        try:
            record = {
                "datetime": stamp,
                "open": float(_bar_value(bar, "open")),
                "high": float(_bar_value(bar, "high")),
                "low": float(_bar_value(bar, "low")),
                "close": float(_bar_value(bar, "close")),
                "volume": float(_bar_value(bar, "volume") or 0.0) * float(volume_multiplier),
            }
        except (TypeError, ValueError):
            logger.debug("Skipping unparsable IB bar: %r", bar)
            continue
        records.append(record)

    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame.from_records(records).set_index("datetime").sort_index()
    df.index = pd.DatetimeIndex(df.index).tz_convert("UTC")
    df.index.name = "datetime"
    # IB pads history with all-zero rows for halted/absent intervals. Those are not
    # real prices, so drop them instead of letting a strategy trade on them.
    zero_mask = (df[["open", "high", "low", "close"]] == 0).all(axis=1)
    if zero_mask.any():
        df = df[~zero_mask]
    return df[~df.index.duplicated(keep="last")]


# --------------------------------------------------------------------------------------
# Pacing
# --------------------------------------------------------------------------------------


class PacingGuard:
    """Enforce IB's historical-data pacing rules.

    * at most ``max_requests_per_10min`` requests in any rolling 10-minute window
    * >= 15 s between two *identical* requests
    * >= 0.35 s between requests for the same contract (IB rejects 6 identical
      contract requests inside 2 s)
    """

    def __init__(
        self,
        max_requests_per_10min: int = DEFAULT_MAX_REQUESTS_PER_10MIN,
        *,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        self.max_requests_per_10min = min(
            max(int(max_requests_per_10min), 1), MAX_REQUESTS_PER_10MIN_CEILING
        )
        self._time = time_func
        self._sleep = sleep_func
        self._lock = threading.Lock()
        self._request_times: List[float] = []
        self._identical: Dict[Tuple, float] = {}
        self._contracts: Dict[str, float] = {}

    def acquire(self, signature: Tuple, contract_key: str) -> None:
        while True:
            with self._lock:
                now = self._time()
                self._request_times = [t for t in self._request_times if now - t < PACING_WINDOW]
                waits = [0.0]
                if len(self._request_times) >= self.max_requests_per_10min:
                    waits.append(PACING_WINDOW - (now - self._request_times[0]))
                last_identical = self._identical.get(signature)
                if last_identical is not None:
                    waits.append(IDENTICAL_REQUEST_COOLDOWN - (now - last_identical))
                last_contract = self._contracts.get(contract_key)
                if last_contract is not None:
                    waits.append(SAME_CONTRACT_COOLDOWN - (now - last_contract))
                wait = max(waits)
                if wait <= 0:
                    self._request_times.append(now)
                    self._identical[signature] = now
                    self._contracts[contract_key] = now
                    return
            logger.debug("IBKR pacing guard sleeping %.2fs", wait)
            self._sleep(wait)

    def penalize(self, seconds: float) -> None:
        """Back off after IB reported a pacing violation."""
        logger.warning("IBKR pacing violation; backing off for %.1fs", seconds)
        self._sleep(seconds)


_pacing_guards: Dict[Tuple[str, int], PacingGuard] = {}
_pacing_guards_lock = threading.Lock()


def get_pacing_guard(host: str, port: int, max_requests_per_10min: int) -> PacingGuard:
    """Process-wide pacing guard shared by every client for one gateway."""
    key = (str(host), int(port))
    with _pacing_guards_lock:
        guard = _pacing_guards.get(key)
        if guard is None:
            guard = PacingGuard(max_requests_per_10min)
            _pacing_guards[key] = guard
        else:
            # The gateway enforces the limit per connection-less 10-minute window, so
            # the strictest budget any client asked for is the one that must win.
            guard.max_requests_per_10min = min(
                guard.max_requests_per_10min,
                min(max(int(max_requests_per_10min), 1), MAX_REQUESTS_PER_10MIN_CEILING),
            )
        return guard


# --------------------------------------------------------------------------------------
# ibapi client
# --------------------------------------------------------------------------------------

_IB_APP_CLASS = None
_IB_APP_CLASS_LOCK = threading.Lock()


def _parse_error_args(args: Sequence[Any]) -> Tuple[Optional[int], str]:
    """Normalise ``EWrapper.error`` args across ibapi 9.81 / 10.x signatures."""
    values = list(args)
    ints = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    strings = [v for v in values if isinstance(v, str)]
    code: Optional[int] = None
    if len(ints) >= 2 and len(values) >= 3 and isinstance(values[0], int) and isinstance(values[1], int):
        # ibapi >= 10.30: (errorTime, errorCode, errorString, ...)
        code = ints[1]
    elif ints:
        code = ints[0]
    message = strings[0] if strings else ""
    return code, message


def _get_ib_app_class():
    global _IB_APP_CLASS
    with _IB_APP_CLASS_LOCK:
        if _IB_APP_CLASS is not None:
            return _IB_APP_CLASS
        try:
            from ibapi.client import EClient
            from ibapi.wrapper import EWrapper
        except ImportError as exc:  # pragma: no cover - ibapi is a hard dependency
            raise IBKRTWSError(
                "The 'ibapi' package is required for the IB Gateway/TWS backtesting data "
                "source. Install it with `pip install ibapi`."
            ) from exc

        class _IBApp(EWrapper, EClient):
            def __init__(self, owner: "IBKRTWSClient"):
                EWrapper.__init__(self)
                EClient.__init__(self, wrapper=self)
                self._owner = owner

            def nextValidId(self, orderId: int):  # noqa: N802 - ibapi callback name
                self._owner._on_next_valid_id(orderId)

            def historicalData(self, reqId, bar):  # noqa: N802
                self._owner._on_bar(reqId, bar)

            def historicalDataEnd(self, reqId, start, end):  # noqa: N802
                self._owner._on_end(reqId)

            def error(self, reqId, *args, **kwargs):  # noqa: N802
                code, message = _parse_error_args(args)
                self._owner._on_error(reqId, code, message)

            def connectionClosed(self):  # noqa: N802
                self._owner._on_connection_closed()

        _IB_APP_CLASS = _IBApp
        return _IB_APP_CLASS


def _build_contract(asset: Asset, config: IBKRTWSConfig):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = str(asset.symbol).upper()
    contract.secType = "STK"
    contract.exchange = config.exchange or "SMART"
    contract.currency = config.currency or "USD"
    if config.primary_exchange:
        contract.primaryExchange = config.primary_exchange
    return contract


@dataclass
class _RequestState:
    req_id: int
    bars: List[Any] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)
    error_code: Optional[int] = None
    error_message: str = ""


class IBKRTWSClient:
    """Thin, reusable ``reqHistoricalData`` client for one gateway connection.

    One instance is owned by a backtesting data source and reused for every
    asset. It connects lazily -- a fully warm cache never constructs one.
    """

    def __init__(self, config: IBKRTWSConfig):
        self.config = config
        self.client_id = int(config.client_id)
        self._app = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connect_lock = threading.RLock()
        self._requests: Dict[int, _RequestState] = {}
        self._cancelled: set = set()
        self._next_req_id = 1
        self._connected_event = threading.Event()
        self._connection_error: Optional[str] = None
        self._pacing_guard = get_pacing_guard(config.host, config.port, config.max_requests_per_10min)
        self.request_count = 0

    # -- ibapi callbacks (invoked on the reader thread) ---------------------------------

    def _on_next_valid_id(self, order_id: int) -> None:
        self._connected_event.set()

    def _on_bar(self, req_id: int, bar: Any) -> None:
        with self._lock:
            state = self._requests.get(req_id)
            if state is None:
                return
            state.bars.append(bar)

    def _on_end(self, req_id: int) -> None:
        with self._lock:
            state = self._requests.get(req_id)
        if state is not None:
            state.done.set()

    def _on_error(self, req_id: int, code: Optional[int], message: str) -> None:
        if code in BENIGN_ERROR_CODES:
            logger.debug("IBKR info %s: %s", code, message)
            return
        if code in CONNECTIVITY_RESTORED_ERROR_CODES:
            logger.info("IBKR connectivity restored (%s): %s", code, message)
            return
        if code in FARM_DEGRADED_ERROR_CODES:
            logger.warning("IBKR data farm degraded (%s): %s", code, message)

        if req_id is None or int(req_id) < 0:
            # Global/system message. Fail an in-flight connection attempt on hard errors.
            if code in CONNECTION_ERROR_CODES:
                self._connection_error = f"IB error {code}: {message}"
                self._connected_event.set()
            elif code in FARM_DEGRADED_ERROR_CODES:
                with self._lock:
                    states = list(self._requests.values())
                for state in states:
                    state.error_code = code
                    state.error_message = message
                    state.done.set()
            return

        with self._lock:
            state = self._requests.get(int(req_id))
        if state is None:
            logger.debug("Ignoring IB error for unknown/cancelled reqId %s: %s %s", req_id, code, message)
            return
        state.error_code = code
        state.error_message = message or ""
        state.done.set()

    def _on_connection_closed(self) -> None:
        # Mark the connection unusable so the next request reconnects instead of
        # writing into a dead socket. Do NOT join the reader thread here: this runs
        # *on* that thread.
        self._connection_error = "Connection to the IB gateway was closed"
        self._connected_event.clear()
        self._app = None
        self._thread = None
        with self._lock:
            states = list(self._requests.values())
            self._requests.clear()
        for state in states:
            if not state.done.is_set():
                state.error_code = 504
                state.error_message = "Connection to the IB gateway was closed"
                state.done.set()

    # -- lifecycle ----------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._app is not None and self._connected_event.is_set() and not self._connection_error

    def connect(self) -> None:
        """Connect and wait for ``nextValidId``; retries on client-id collisions."""
        # Two threads racing here would each build an EClient and a reader thread,
        # breaking the one-connection-per-datasource invariant and orphaning a socket.
        with self._connect_lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        if self.is_connected:
            return
        app_class = _get_ib_app_class()
        last_error: Optional[str] = None
        for attempt in range(3):
            client_id = self.client_id + attempt
            self._connected_event.clear()
            self._connection_error = None
            app = app_class(self)
            try:
                app.connect(self.config.host, int(self.config.port), client_id)
            except Exception as exc:
                last_error = str(exc)
                self._safe_disconnect(app, None)
                continue

            thread = threading.Thread(
                target=app.run, name=f"ibkr-tws-reader-{client_id}", daemon=True
            )
            thread.start()
            if self._connected_event.wait(timeout=self.config.timeout) and not self._connection_error:
                self._app = app
                self._thread = thread
                self.client_id = client_id
                logger.info(
                    "Connected to IB gateway at %s:%s with client id %s",
                    self.config.host,
                    self.config.port,
                    client_id,
                )
                return
            last_error = self._connection_error or "timed out waiting for nextValidId"
            self._safe_disconnect(app, thread)
            if self._connection_error and "326" not in str(self._connection_error):
                break

        raise IBKRTWSConnectionError(
            f"Could not connect to the IB Gateway/TWS socket API at {self.config.host}:"
            f"{self.config.port} (client id {self.client_id}): {last_error}. "
            "Check that IB Gateway or TWS is running, that its API is enabled with this "
            "host allowed, and that INTERACTIVE_BROKERS_PORT matches the gateway "
            "(4002 = Gateway paper, 4001 = Gateway live, 7497 = TWS paper, 7496 = TWS live). "
            "If the client id is already in use, set IBKR_BACKTEST_CLIENT_ID to a free value."
        )

    @staticmethod
    def _safe_disconnect(app, thread: Optional[threading.Thread]) -> None:
        try:
            app.disconnect()
        except Exception:  # pragma: no cover - best effort teardown
            logger.debug("Ignoring error while disconnecting IB app", exc_info=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def close(self) -> None:
        """Cancel outstanding requests, disconnect and join the reader thread."""
        with self._connect_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        app, thread = self._app, self._thread
        self._app, self._thread = None, None
        if app is None:
            return
        try:
            with self._lock:
                req_ids = list(self._requests)
            for req_id in req_ids:
                try:
                    app.cancelHistoricalData(req_id)
                except Exception:  # pragma: no cover - best effort
                    logger.debug("Ignoring cancelHistoricalData error for %s", req_id, exc_info=True)
        finally:
            self._safe_disconnect(app, thread)
            with self._lock:
                self._requests.clear()
            self._connected_event.clear()

    def __enter__(self) -> "IBKRTWSClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- requests -----------------------------------------------------------------------

    def _reserve_req_id(self) -> int:
        with self._lock:
            req_id = self._next_req_id
            self._next_req_id += 1
            return req_id

    def request_historical_bars(
        self,
        contract,
        end_datetime: datetime,
        duration: str,
        bar_size: str,
        *,
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        timeout: Optional[float] = None,
        max_pacing_retries: int = 3,
    ) -> List[Any]:
        """Run one ``reqHistoricalData`` call and return its bars.

        Raises
        ------
        IBKRTWSContractError, IBKRTWSPermissionError, IBKRTWSConnectionError,
        IBKRTWSTimeoutError
            For the corresponding IB failure classes.
        IBKRTWSError
            For "no data" responses the caller should treat as authoritative
            emptiness only when the IB message says so.
        """
        self.connect()
        timeout = float(timeout if timeout is not None else self.config.timeout)
        contract_key = f"{contract.symbol}|{contract.secType}|{contract.exchange}|{contract.currency}"
        use_utc_suffix = True

        for attempt in range(max_pacing_retries + 1):
            end_str = _format_end_datetime(end_datetime, with_timezone=use_utc_suffix)
            signature = (contract_key, end_str, duration, bar_size, what_to_show, bool(use_rth))
            self._pacing_guard.acquire(signature, contract_key)

            req_id = self._reserve_req_id()
            state = _RequestState(req_id=req_id)
            with self._lock:
                self._requests[req_id] = state
            self.request_count += 1

            # Snapshot the app: close() and connectionClosed() both clear self._app,
            # and pacing can hold this call for many seconds before it is sent.
            app = self._app
            if app is None:
                with self._lock:
                    self._requests.pop(req_id, None)
                raise IBKRTWSConnectionError(
                    "The connection to the IB gateway was closed before the historical "
                    "data request could be sent."
                )

            try:
                app.reqHistoricalData(
                    req_id,
                    contract,
                    end_str,
                    duration,
                    bar_size,
                    what_to_show,
                    1 if use_rth else 0,
                    2,  # formatDate=2 -> epoch seconds (unambiguous)
                    False,
                    [],
                )
            except Exception as exc:
                with self._lock:
                    self._requests.pop(req_id, None)
                raise IBKRTWSConnectionError(
                    f"Failed to send a historical data request to the IB gateway: {exc}"
                ) from exc

            completed = state.done.wait(timeout=timeout)
            with self._lock:
                self._requests.pop(req_id, None)

            if not completed:
                try:
                    app.cancelHistoricalData(req_id)
                except Exception:  # pragma: no cover - best effort
                    logger.debug("cancelHistoricalData failed for %s", req_id, exc_info=True)
                raise IBKRTWSTimeoutError(
                    f"Timed out after {timeout:.0f}s waiting for IB historical data "
                    f"({contract.symbol} {bar_size} {duration} ending {end_str}). "
                    "The range was left uncached so it will be retried."
                )

            code, message = state.error_code, state.error_message
            if code is None:
                return state.bars

            if code == INVALID_END_DATETIME_ERROR_CODE and use_utc_suffix:
                logger.debug("IB rejected the UTC endDateTime suffix; retrying without it.")
                use_utc_suffix = False
                continue

            lowered = (message or "").lower()
            if code == HISTORICAL_SERVICE_ERROR_CODE and "pacing violation" in lowered:
                if attempt >= max_pacing_retries:
                    raise IBKRTWSPacingViolation(f"IB error {code}: {message}")
                backoff = 15.0 * (2**attempt)
                self._pacing_guard.penalize(backoff)
                continue

            raise _classify_ib_error(code, message, contract)

        raise IBKRTWSPacingViolation(
            f"Gave up after {max_pacing_retries} pacing retries for {contract.symbol}."
        )


def _classify_ib_error(code: Optional[int], message: str, contract) -> IBKRTWSError:
    lowered = (message or "").lower()
    symbol = getattr(contract, "symbol", "?")
    if code in CONTRACT_ERROR_CODES:
        return IBKRTWSContractError(
            f"IB could not resolve contract {symbol} "
            f"({getattr(contract, 'secType', '?')}/{getattr(contract, 'exchange', '?')}/"
            f"{getattr(contract, 'currency', '?')}) - IB error {code}: {message}"
        )
    if code in PERMISSION_ERROR_CODES:
        return IBKRTWSPermissionError(
            f"The IB account is not entitled to this historical data for {symbol} "
            f"- IB error {code}: {message}"
        )
    if code in CONNECTION_ERROR_CODES or code in FARM_DEGRADED_ERROR_CODES:
        return IBKRTWSConnectionError(f"IB connection problem for {symbol} - IB error {code}: {message}")
    if code == HISTORICAL_SERVICE_ERROR_CODE:
        if _is_authoritative_empty(message):
            return _NoDataSignal(f"IB error {code}: {message}")
        return IBKRTWSError(f"IB historical data service error for {symbol} - IB error {code}: {message}")
    if "no data" in lowered:
        return _NoDataSignal(f"IB error {code}: {message}")
    return IBKRTWSError(f"IB error {code} for {symbol}: {message}")


class _NoDataSignal(IBKRTWSError):
    """IB authoritatively reported that the requested range holds no data."""


def _is_authoritative_empty(message: str) -> bool:
    lowered = (message or "").lower()
    return (
        "hmds query returned no data" in lowered
        or "no historical market data" in lowered
        or "no data of type" in lowered
    )


def _format_end_datetime(value: datetime, *, with_timezone: bool = True) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if with_timezone:
        return value.astimezone(timezone.utc).strftime("%Y%m%d %H:%M:%S") + " UTC"
    return value.astimezone(_EASTERN).strftime("%Y%m%d %H:%M:%S")


# --------------------------------------------------------------------------------------
# Chunk download
# --------------------------------------------------------------------------------------

CHUNK_COMPLETE = "complete"
CHUNK_EMPTY = "empty"
CHUNK_FAILED = "failed"


@dataclass
class ChunkResult:
    """Outcome of a single IB historical request.

    ``status`` drives whether the chunk's sessions may be negatively cached:
    only ``complete`` and ``empty`` are authoritative.
    """

    status: str
    first_date: date
    last_date: date
    bars: List[Any] = field(default_factory=list)
    message: str = ""

    @property
    def authoritative(self) -> bool:
        return self.status in (CHUNK_COMPLETE, CHUNK_EMPTY)

    def authoritative_sessions(
        self, candidate_sessions: Sequence[date], earliest_returned: Optional[date]
    ) -> set:
        """Sessions this chunk proves are genuinely empty.

        IB anchors a response on ``endDateTime`` and walks *backwards*, so a
        response capped at the bar limit loses its **oldest** bars. A chunk that
        returned data is therefore only authoritative for sessions strictly newer
        than its oldest returned bar; anything older may simply have been
        truncated away and must stay retryable. A chunk that returned nothing at
        all (or that IB explicitly reported as empty) is authoritative for its
        whole range.
        """
        if not self.authoritative:
            return set()
        in_range = {d for d in candidate_sessions if self.first_date <= d <= self.last_date}
        if self.status == CHUNK_EMPTY:
            return in_range
        if earliest_returned is None:
            # The chunk returned bars but none of them yielded a usable timestamp
            # (all-zero rows, unparsable dates). That is not proof of emptiness, so
            # nothing here may be placeholdered.
            return set()
        return {d for d in in_range if d > earliest_returned}

def _earliest_raw_session(
    bars: Sequence[Any],
    timespan: str,
    session_closes: Optional[Dict[date, pd.Timestamp]] = None,
) -> Optional[date]:
    """Eastern session date of the oldest usable, non-zero OHLC bar in a response.

    Zero-padded rows are not evidence that IB returned real history for that
    session. If a response contains only those rows, return ``None`` so the caller
    cannot write permanent no-data placeholders based on the padding.
    """
    earliest: Optional[date] = None
    for bar in bars:
        try:
            ohlc = tuple(float(_bar_value(bar, name)) for name in ("open", "high", "low", "close"))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in ohlc) or all(value == 0 for value in ohlc):
            continue
        ts = parse_ib_datetime(_bar_value(bar, "date"), timespan, session_closes)
        if ts is None:
            continue
        session_date = ts.tz_convert(_EASTERN).date()
        if earliest is None or session_date < earliest:
            earliest = session_date
    return earliest


def _download_chunk(
    client: IBKRTWSClient,
    contract,
    chunk: Tuple[date, date],
    *,
    bar_size: str,
    config: IBKRTWSConfig,
    sessions: pd.DataFrame,
) -> ChunkResult:
    first_date, last_date = chunk
    close_ts = sessions["market_close"].get(last_date)
    if close_ts is None:
        end_dt = _EASTERN.localize(
            datetime(last_date.year, last_date.month, last_date.day, 16, 0)
        ).astimezone(timezone.utc)
    else:
        end_dt = pd.Timestamp(close_ts).tz_convert("UTC").to_pydatetime()
    # Nudge past the close so the final bar of the session is included.
    end_dt = end_dt + timedelta(minutes=1)

    try:
        bars = client.request_historical_bars(
            contract,
            end_dt,
            format_duration(first_date, last_date),
            bar_size,
            what_to_show=config.what_to_show,
            use_rth=config.use_rth,
            timeout=config.timeout,
        )
    except _NoDataSignal as exc:
        logger.info("IB reported no data for %s %s..%s: %s", contract.symbol, first_date, last_date, exc)
        return ChunkResult(CHUNK_EMPTY, first_date, last_date, message=str(exc))
    except (IBKRTWSContractError, IBKRTWSPermissionError):
        raise
    except IBKRTWSError:
        # Timeouts, connection loss, exhausted pacing retries: the range stays
        # uncached, and the caller persists whatever succeeded before re-raising.
        # Swallowing these would hand the strategy a silently incomplete history.
        raise

    if not bars:
        return ChunkResult(CHUNK_EMPTY, first_date, last_date)
    return ChunkResult(CHUNK_COMPLETE, first_date, last_date, bars=list(bars))


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


def _merge_frames(df_all: Optional[pd.DataFrame], df_new: pd.DataFrame) -> pd.DataFrame:
    if df_new is None or df_new.empty:
        return df_all if df_all is not None else pd.DataFrame()
    df_new = _normalize_index(df_new)
    if df_all is None or df_all.empty:
        return df_new
    combined = pd.concat([_normalize_index(df_all), df_new]).sort_index()
    return combined[~combined.index.duplicated(keep="last")]


def _finalize(df_all: Optional[pd.DataFrame], start: datetime, end: datetime, timespan: str) -> pd.DataFrame:
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if df_all is None or df_all.empty:
        return empty
    df = _real_rows(_normalize_index(df_all))
    if df.empty:
        return empty
    df = df.drop(columns=[c for c in ("missing",) if c in df.columns])
    df = df.dropna(how="all")
    if df.empty:
        return empty

    start_utc, end_utc = _to_utc_bounds(start, end)
    if timespan == "day":
        # Day bars are stamped at the session close, so an inclusive date-level
        # filter keeps the caller's final session.
        index_dates = pd.DatetimeIndex(df.index).tz_convert(_EASTERN).date
        mask = (index_dates >= start_utc.astimezone(_EASTERN).date()) & (
            index_dates <= end_utc.astimezone(_EASTERN).date()
        )
        return df[mask]
    return df[(df.index >= start_utc) & (df.index <= end_utc)]


def _to_utc_bounds(start: datetime, end: datetime) -> Tuple[pd.Timestamp, pd.Timestamp]:
    def _coerce(value, end_of_day: bool) -> pd.Timestamp:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, date):
            dt = datetime.combine(value, datetime.max.time() if end_of_day else datetime.min.time())
        else:
            dt = pd.Timestamp(value).to_pydatetime()
        if end_of_day and isinstance(value, datetime) and value.time() == datetime.min.time():
            dt = datetime.combine(value.date(), datetime.max.time())
            if value.tzinfo is not None:
                dt = dt.replace(tzinfo=value.tzinfo)
        if dt.tzinfo is None:
            dt = _EASTERN.localize(dt)
        return pd.Timestamp(dt).tz_convert("UTC")

    return _coerce(start, False), _coerce(end, True)


def get_price_data_from_ibkr_tws(
    asset: Asset,
    start: datetime,
    end: datetime,
    timespan: str = "minute",
    quote_asset: Optional[Asset] = None,
    force_cache_update: bool = False,
    *,
    config: Optional[IBKRTWSConfig] = None,
    client: Optional[IBKRTWSClient] = None,
    client_factory: Optional[Callable[[IBKRTWSConfig], IBKRTWSClient]] = None,
    show_progress: bool = True,
    now: Optional[datetime] = None,
) -> pd.DataFrame:
    """Download historical bars for ``asset`` from an IB Gateway / TWS socket API.

    Mirrors :func:`lumibot.tools.polygon_helper.get_price_data_from_polygon`: data
    is cached as parquet under ``LUMIBOT_CACHE_FOLDER/ibkr_tws`` and only
    uncovered trading sessions are downloaded. When the cache already covers the
    request, **no client is created and no network call is made**.

    Parameters
    ----------
    asset : Asset
        Stock/ETF to fetch. Other asset types raise :class:`NotImplementedError`.
    start, end : datetime
        Requested range (naive datetimes are interpreted as America/New_York).
    timespan : str
        ``"minute"`` or ``"day"``.
    quote_asset : Asset, optional
        Only used to disambiguate the cache filename.
    force_cache_update : bool
        Ignore any cached data and re-download the whole range.
    config : IBKRTWSConfig, optional
        Connection/request settings; defaults to :meth:`IBKRTWSConfig.from_env`.
    client : IBKRTWSClient, optional
        A client owned by the caller. It is reused and **not** closed here.
    client_factory : callable, optional
        Builds a client when ``client`` is ``None``. Injected by tests.
    show_progress : bool
        Display a tqdm progress bar for the downloads.
    now : datetime, optional
        Override "current time" (tests). Sessions that have not closed yet are
        never negatively cached.

    Returns
    -------
    pandas.DataFrame
        UTC-indexed ``open/high/low/close/volume`` frame, placeholder rows removed.
        May be empty; it is never padded with synthetic bars.
    """
    validate_asset(asset)
    timespan = str(timespan or "").strip().lower()
    bar_size = validate_timespan(timespan)
    config = config or IBKRTWSConfig.from_env()
    validate_quote_asset(quote_asset, config)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    volume_multiplier = config.effective_volume_multiplier(asset)
    cache_file = build_cache_filename(
        asset,
        timespan,
        quote_asset,
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
        primary_exchange=config.primary_exchange,
    )
    meta = _build_meta(
        asset,
        timespan,
        what_to_show=config.what_to_show,
        use_rth=config.use_rth,
        exchange=config.exchange,
        currency=config.currency,
        primary_exchange=config.primary_exchange,
        volume_multiplier=volume_multiplier,
    )

    df_all: Optional[pd.DataFrame] = None
    if not force_cache_update and _meta_matches(cache_file, meta):
        df_all = load_cache(cache_file)

    sessions = get_trading_sessions(start, end)
    if sessions.empty:
        return _finalize(df_all, start, end, timespan)

    session_closes = _session_close_lookup(sessions)
    missing = compute_missing_sessions(
        df_all, sessions, timespan, use_rth=config.use_rth, now=now_utc
    )
    if not missing:
        # Fully warm cache: zero network calls, no client construction.
        return _finalize(df_all, start, end, timespan)

    chunk_days = config.minute_chunk_days if timespan == "minute" else config.day_chunk_days
    chunks = build_chunks(missing, timespan, chunk_days)

    own_client = client is None
    if own_client:
        factory = client_factory or IBKRTWSClient
        client = factory(config)

    authoritative_sessions: set = set()
    download_error: Optional[BaseException] = None
    try:
        contract = _build_contract(asset, config)
        pbar = None
        if show_progress and chunks:
            pbar = tqdm(
                total=len(chunks),
                desc=f"Downloading {asset.symbol} '{timespan}' bars from IB Gateway/TWS",
                dynamic_ncols=True,
            )
        try:
            for chunk in chunks:
                result = _download_chunk(
                    client,
                    contract,
                    chunk,
                    bar_size=bar_size,
                    config=config,
                    sessions=sessions,
                )
                earliest_returned: Optional[date] = None
                if result.bars:
                    earliest_returned = _earliest_raw_session(
                        result.bars, timespan, session_closes
                    )
                    df_new = parse_bars(
                        result.bars,
                        timespan,
                        session_closes=session_closes,
                        volume_multiplier=volume_multiplier,
                    )
                    if timespan == "day" and not df_new.empty:
                        # IB's duration slack may include a prior session. It is
                        # outside this request and may lack its actual (possibly
                        # early) close in session_closes, so do not cache it.
                        requested_dates = set(sessions.index)
                        returned_dates = df_new.index.tz_convert(_EASTERN).date
                        df_new = df_new[
                            [session_date in requested_dates for session_date in returned_dates]
                        ]
                    df_all = _merge_frames(df_all, df_new)
                authoritative_sessions.update(
                    result.authoritative_sessions(missing, earliest_returned)
                )
                if earliest_returned is not None and earliest_returned > result.first_date:
                    logger.debug(
                        "IB returned no bars before %s for %s %s..%s; those sessions stay "
                        "retryable in case the response was truncated at the bar cap.",
                        earliest_returned,
                        asset.symbol,
                        result.first_date,
                        result.last_date,
                    )
                if pbar is not None:
                    pbar.update(1)
        except IBKRTWSError as exc:
            # Persist whatever already downloaded so the next run resumes instead of
            # starting over, then re-raise: a silently short history is worse than a
            # loud failure.
            download_error = exc
        finally:
            if pbar is not None:
                pbar.close()
    finally:
        if own_client:
            client.close()

    still_missing = compute_missing_sessions(
        df_all, sessions, timespan, use_rth=config.use_rth, now=now_utc
    )
    # Only sessions that a *successful* request covered, and that have already
    # closed, may be marked as authoritatively empty.
    placeholders = [
        d
        for d in still_missing
        if d in authoritative_sessions and _session_is_closed(session_closes.get(d), d, now_utc)
    ]
    unresolved = [d for d in still_missing if d not in set(placeholders)]
    df_all = update_cache(
        cache_file,
        df_all,
        placeholders,
        session_closes=session_closes,
        meta=meta,
        replace_existing=force_cache_update and download_error is None and not unresolved,
    )

    if unresolved:
        logger.warning(
            "IB Gateway/TWS history for %s (%s) is still missing %d session(s) "
            "(e.g. %s); they were left uncached and will be retried on the next run.",
            asset.symbol,
            timespan,
            len(unresolved),
            unresolved[:3],
        )

    if download_error is not None:
        raise download_error

    return _finalize(df_all, start, end, timespan)


def _session_is_closed(close_ts, session_date: date, now_utc: datetime) -> bool:
    if close_ts is None:
        return session_date < now_utc.astimezone(_EASTERN).date()
    return pd.Timestamp(close_ts).tz_convert("UTC").to_pydatetime() <= now_utc
