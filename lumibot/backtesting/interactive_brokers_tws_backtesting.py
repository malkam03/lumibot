"""Backtesting data source backed by an IB Gateway / TWS socket API connection."""

from __future__ import annotations

import math
import threading
import traceback
from collections import OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Union

import pandas as pd

from lumibot.data_sources import PandasData
from lumibot.entities import Asset, Data
from lumibot.tools import ibkr_tws_helper
from lumibot.tools.ibkr_tws_helper import IBKRTWSClient, IBKRTWSConfig
from lumibot.tools.lumibot_logger import get_logger

logger = get_logger(__name__)

START_BUFFER = timedelta(days=5)


class InteractiveBrokersTWSBacktesting(PandasData):
    """Backtest against historical bars pulled straight from IB Gateway / TWS.

    Unlike :class:`~lumibot.backtesting.interactive_brokers_rest_backtesting.InteractiveBrokersRESTBacktesting`,
    which goes through LumiWealth's hosted Data Downloader service, this data
    source speaks the Interactive Brokers *socket* API (the "TWS API") directly
    via ``ibapi.EClient.reqHistoricalData``. All you need is a running IB Gateway
    or TWS with the API enabled.

    Bars are cached as parquet under ``LUMIBOT_CACHE_FOLDER/ibkr_tws``, so the
    second run of a backtest makes no network calls at all.

    Scope: stocks/ETFs, ``minute`` and ``day`` timesteps. Options, futures, forex
    and crypto raise :class:`NotImplementedError`.

    Examples
    --------
    >>> from lumibot.backtesting import InteractiveBrokersTWSBacktesting
    >>> MyStrategy.backtest(
    ...     InteractiveBrokersTWSBacktesting,
    ...     datetime(2024, 1, 1),
    ...     datetime(2024, 3, 1),
    ... )
    """

    # SOURCE is deliberately inherited from PandasData ("PANDAS"), as Polygon,
    # ThetaData and DataBento do. BacktestingBroker only routes pending orders into
    # its OHLC fill model when SOURCE == "PANDAS" (or a hard-coded provider name);
    # a custom SOURCE here left every order pending forever and nothing ever filled.
    MIN_TIMESTEP = "minute"
    # IB day bars need native daily data: they are re-stamped at the session close
    # to avoid same-session lookahead, which minute->day resampling would undo.
    PREFER_NATIVE_DAY_BARS_FOR_STOCK_INDEX = True
    # Daily-cadence strategies should price from native day bars instead of pulling
    # years of minute bars through IB pacing. Safe because day bars are stamped at the
    # session close, so the latest bar at or before sim time never leaks the future.
    SUPPORTS_DAILY_LAST_PRICE_OPTIMIZATION = True
    # Preference order when a caller does not name a timestep: the finest data wins.
    _UNSPECIFIED_TIMESTEP_LOOKUP_ORDER = ("minute", "day")

    def __init__(
        self,
        datetime_start,
        datetime_end,
        pandas_data=None,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
        what_to_show: Optional[str] = None,
        use_rth: Optional[bool] = None,
        timeout: Optional[float] = None,
        exchange: Optional[str] = None,
        currency: Optional[str] = None,
        volume_multiplier: Optional[float] = None,
        config: Optional[IBKRTWSConfig] = None,
        client: Optional[IBKRTWSClient] = None,
        max_memory: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(
            datetime_start=datetime_start,
            datetime_end=datetime_end,
            pandas_data=pandas_data,
            **kwargs,
        )

        self.MAX_STORAGE_BYTES = max_memory
        # Strategy.run_backtest() forwards its generic config dict to each data
        # source. Only accept this source's typed config; otherwise use its own
        # explicit arguments and environment rather than treating a dict as IB config.
        self.ibkr_config = config if isinstance(config, IBKRTWSConfig) else IBKRTWSConfig.from_env(
            host=host,
            port=port,
            client_id=client_id,
            what_to_show=(what_to_show.strip().upper() if what_to_show else None),
            use_rth=use_rth,
            timeout=timeout,
            exchange=exchange,
            currency=currency,
            volume_multiplier=volume_multiplier,
        )
        # One connection per data source instance, shared across every asset and
        # created lazily so a fully warm cache never opens a socket.
        self._client = client
        self._owns_client = client is None
        self._client_lock = threading.Lock()
        self.data_source = self

    # -- connection lifecycle -------------------------------------------------------

    def _get_client(self) -> IBKRTWSClient:
        # Locked so two concurrent asset lookups cannot each build a connection.
        with self._client_lock:
            if self._client is None:
                self._client = IBKRTWSClient(self.ibkr_config)
                self._owns_client = True
            return self._client

    def close(self) -> None:
        """Disconnect from the gateway (safe to call more than once)."""
        with self._client_lock:
            client, self._client = self._client, None
            owns = self._owns_client
        if client is not None and owns:
            client.close()

    def __enter__(self) -> "InteractiveBrokersTWSBacktesting":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - interpreter shutdown ordering
        try:
            self.close()
        except Exception:
            pass

    # -- storage --------------------------------------------------------------------

    def _enforce_storage_limit(self, pandas_data: OrderedDict) -> None:
        storage_used = sum(data.df.memory_usage().sum() for data in pandas_data.values())
        logger.debug("%s bytes used for %s items", f"{storage_used:,}", len(pandas_data))
        while storage_used > self.MAX_STORAGE_BYTES and pandas_data:
            key, data = pandas_data.popitem(last=False)
            freed = data.df.memory_usage().sum()
            storage_used -= freed
            logger.info("Storage limit exceeded. Evicted LRU data: %s used %s bytes", key, f"{freed:,}")

    # -- data ------------------------------------------------------------------------

    def find_asset_in_data_store(self, asset, quote=None, timestep=None):
        """Resolve the canonical ``(asset, quote, timestep)`` keys this source writes.

        Inherited ``PandasData.get_last_price()`` and ``get_quote()`` look up without
        a timestep, and the base implementation only builds timestep-bearing
        candidate keys when a timestep is given. Without this fallback every loaded
        dataset is unreachable from those methods and no order can ever fill.
        """
        key = super().find_asset_in_data_store(asset, quote, timestep)
        if key is not None or timestep is not None:
            return key
        for candidate_timestep in self._UNSPECIFIED_TIMESTEP_LOOKUP_ORDER:
            key = super().find_asset_in_data_store(asset, quote, candidate_timestep)
            if key is not None:
                return key
        return None

    def _update_pandas_data(self, asset, quote, length, timestep, start_dt=None):
        """Download (or reuse cached) bars and merge them into ``self.pandas_data``."""
        search_asset = asset
        asset_separated = asset
        quote_asset = quote if quote is not None else Asset("USD", "forex")

        if isinstance(search_asset, tuple):
            asset_separated, quote_asset = search_asset
        else:
            search_asset = (search_asset, quote_asset)

        start_datetime, ts_unit = self.get_start_datetime_and_ts_unit(
            length, timestep, start_dt, start_buffer=START_BUFFER
        )

        # Key datasets by (asset, quote, timestep) so minute and day data for the same
        # asset can coexist. `PandasData.find_asset_in_data_store()` understands this
        # canonical key, and with PREFER_NATIVE_DAY_BARS_FOR_STOCK_INDEX a day request
        # must be able to reach a native day dataset even after minute data was loaded.
        dataset_key = (asset_separated, quote_asset, ts_unit)

        asset_data = self.pandas_data.get(dataset_key)
        if asset_data is None:
            legacy = self.pandas_data.get(search_asset)
            if legacy is not None and legacy.timestep == ts_unit:
                asset_data = legacy
        if asset_data is not None and not asset_data.df.empty:
            data_start_datetime = asset_data.df.index[0]
            requested_start = pd.Timestamp(start_datetime)
            if requested_start.tzinfo is None:
                requested_start = requested_start.tz_localize("America/New_York")
            else:
                requested_start = requested_start.tz_convert("America/New_York")
            start_is_covered = data_start_datetime <= (
                requested_start.tz_convert("UTC") + START_BUFFER
            )
            if start_is_covered:
                # The helper's parquet cache may already contain more history than
                # this in-memory Data object (for example, a later backtest extends
                # its end date). Do not skip the helper unless every requested
                # session is adequately covered; the helper will fetch any missing
                # sessions while preserving cached history.
                sessions = ibkr_tws_helper.get_trading_sessions(start_datetime, self.datetime_end)
                if sessions.empty:
                    return
                missing_sessions = ibkr_tws_helper.compute_missing_sessions(
                    asset_data.df,
                    sessions,
                    ts_unit,
                    use_rth=self.ibkr_config.use_rth,
                )
                if not missing_sessions:
                    return

        try:
            df = ibkr_tws_helper.get_price_data_from_ibkr_tws(
                asset_separated,
                start_datetime,
                self.datetime_end,
                timespan=ts_unit,
                quote_asset=quote_asset,
                config=self.ibkr_config,
                client=self._get_client(),
            )
        except NotImplementedError:
            raise
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise Exception(
                f"Error getting data from the IB Gateway/TWS socket API for {asset_separated}"
            ) from exc

        if df is None or df.empty:
            return

        data = Data(asset_separated, df, timestep=ts_unit, quote=quote_asset)
        self.pandas_data[dataset_key] = data
        self.pandas_data.move_to_end(dataset_key)
        self._data_store = self.pandas_data
        # A lookup resolved before this load (to a coarser dataset, or under another
        # quote/timestep spelling) must not hide the dataset just loaded. Loads are
        # rare relative to lookups, so clearing the whole cache is cheap.
        self._find_asset_in_data_store_cache.clear()
        if self.MAX_STORAGE_BYTES:
            self._enforce_storage_limit(self.pandas_data)

    def _pull_source_symbol_bars(
        self,
        asset: Asset,
        length: int,
        timestep: str = "day",
        timeshift: int = None,
        quote: Asset = None,
        exchange: str = None,
        include_after_hours: bool = True,
    ):
        self._update_pandas_data(asset, quote, length, timestep, self.get_datetime())
        return super()._pull_source_symbol_bars(
            asset, length, timestep, timeshift, quote, exchange, include_after_hours
        )

    def get_historical_prices_between_dates(
        self,
        asset,
        timestep="minute",
        quote=None,
        exchange=None,
        include_after_hours=True,
        start_date=None,
        end_date=None,
    ):
        self._update_pandas_data(asset, quote, 1, timestep)
        response = super()._pull_source_symbol_bars_between_dates(
            asset, timestep, quote, exchange, include_after_hours, start_date, end_date
        )
        if response is None:
            return None
        return self._parse_source_symbol_bars(response, asset, quote=quote)

    def get_last_price(
        self, asset, timestep="minute", quote=None, exchange=None, **kwargs
    ) -> Union[float, Decimal, None]:
        # Deliberately not swallowed: a download failure here would otherwise make the
        # backtest price off whatever stale data happens to be loaded.
        self._update_pandas_data(asset, quote, 1, timestep, self.get_datetime())
        key = self.find_asset_in_data_store(asset, quote, timestep)
        if key is None or key not in self._data_store:
            return None

        try:
            data = self._data_store[key]
            now = self.get_datetime()
            price = data.get_last_price(now)
            price = self._adjust_stale_daily_price_for_stock_split(data, price, now)
            if price is None:
                return None
            numeric_price = float(price)
            if not math.isfinite(numeric_price) or numeric_price <= 0:
                logger.warning(
                    "Ignoring invalid or non-positive price %s for %s; treating as missing data.",
                    price,
                    key,
                )
                return None
            return price
        except Exception as exc:
            logger.info("Error getting last price for %s: %s", key, exc)
            return None

    def get_chains(self, asset: Asset, quote: Asset = None, exchange: str = None):
        raise NotImplementedError(
            "Option chains are not supported by the IB Gateway/TWS backtesting data source. "
            "Use PolygonDataBacktesting or ThetaDataBacktesting for options backtests."
        )
