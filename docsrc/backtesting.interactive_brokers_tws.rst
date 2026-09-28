.. _backtesting.interactive_brokers_tws:

Interactive Brokers Gateway / TWS Backtesting
=============================================

.. meta::
   :description: Backtest stock and ETF strategies in LumiBot using historical bars downloaded directly from your own Interactive Brokers Gateway or TWS socket API, with local parquet caching.

``InteractiveBrokersTWSBacktesting`` downloads historical bars **directly from an
Interactive Brokers Gateway or Trader Workstation instance that you run
yourself**, using the official IB socket API (``ibapi`` / ``reqHistoricalData``).

This is different from :doc:`IBKR REST backtesting <backtesting.ibkr>`, which
talks to LumiWealth's hosted Data Downloader service. This source needs no
hosted service and no extra subscription beyond the IB market-data permissions
already attached to your IB account.

.. note::

   Downloaded bars are cached as parquet files under
   ``LUMIBOT_CACHE_FOLDER/ibkr_tws``. The first download of a long intraday range
   is slow (IB pacing limits apply), but every later backtest over the same range
   runs with **zero** network calls.

Requirements
------------

1. An Interactive Brokers account with market-data permissions for the symbols
   you want (a paper account works).
2. A running IB Gateway or TWS with the API enabled
   (*Configure → API → Settings → Enable ActiveX and Socket Clients*), and
   LumiBot's host added to the trusted IPs.
3. ``ibapi``, which ships as a LumiBot dependency — nothing extra to install.

Scope of version 1
------------------

* **Supported:** stocks and ETFs (``secType=STK``, routed ``SMART``, currency
  ``USD``), at ``minute`` and ``day`` timesteps.
* **Not supported:** options, futures, continuous futures, forex, and crypto.
  Requesting them raises a clear ``NotImplementedError``.

Environment variables
---------------------

.. list-table::
   :header-rows: 1
   :widths: 34 16 50

   * - Variable
     - Default
     - Meaning
   * - ``INTERACTIVE_BROKERS_IP``
     - ``127.0.0.1``
     - Host running IB Gateway or TWS. Shared with the live IB broker.
   * - ``INTERACTIVE_BROKERS_PORT``
     - ``4002``
     - API port (4002 Gateway paper, 4001 Gateway live, 7497 TWS paper, 7496 TWS live).
   * - ``IBKR_BACKTEST_CLIENT_ID``
     - ``77``
     - Dedicated API client id for backtest downloads. Kept high on purpose so it
       never collides with the client ids (1–11) used by live strategies.
   * - ``IBKR_BACKTEST_WHAT_TO_SHOW``
     - ``TRADES``
     - IB ``whatToShow`` value.
   * - ``IBKR_BACKTEST_USE_RTH``
     - ``1``
     - ``1`` restricts bars to regular trading hours; ``0`` includes extended hours.
   * - ``IBKR_BACKTEST_TIMEOUT``
     - ``120``
     - Seconds to wait for a single historical-data response.
   * - ``IBKR_BACKTEST_MAX_REQUESTS_PER_10MIN``
     - ``55``
     - Client-side pacing budget. IB's hard limit is 60 historical requests per
       10 minutes; the default leaves headroom.

Quickstart
----------

.. code-block:: python

    from datetime import datetime

    from lumibot.backtesting import InteractiveBrokersTWSBacktesting
    from lumibot.strategies import Strategy


    class MyStrategy(Strategy):
        parameters = {"symbol": "SPY"}

        def initialize(self):
            self.sleeptime = "1D"

        def on_trading_iteration(self):
            if self.first_iteration:
                symbol = self.parameters["symbol"]
                price = self.get_last_price(symbol)
                qty = self.portfolio_value // price
                self.submit_order(self.create_order(symbol, quantity=qty, side="buy"))


    if __name__ == "__main__":
        MyStrategy.run_backtest(
            InteractiveBrokersTWSBacktesting,
            datetime(2024, 1, 1),
            datetime(2024, 3, 1),
        )

You can also select the source without touching your code:

.. code-block:: bash

    export BACKTESTING_DATA_SOURCE=ibkr_tws

The alias ``interactive_brokers_tws`` resolves to the same class. Plain
``ibkr`` still means the REST Data Downloader source, which is unchanged.

Connection settings can also be passed directly, which overrides the
environment:

.. code-block:: python

    MyStrategy.run_backtest(
        InteractiveBrokersTWSBacktesting,
        datetime(2024, 1, 1),
        datetime(2024, 3, 1),
        host="10.0.0.20",
        port=4001,
        client_id=77,
        use_rth=True,
    )

Bar sizes and chunking
----------------------

============  =====================  ==================================
Timestep      IB ``barSizeSetting``  Request window
============  =====================  ==================================
``minute``    ``1 min``              14 calendar days per request
``day``       ``1 day``              365 calendar days per request
============  =====================  ==================================

IB caps a single response at roughly 8,190 bars, which is exactly 21 regular
sessions of 1-minute RTH data. A calendar month can contain 22 sessions, so a
``"1 M"`` minute request would silently drop its oldest session. LumiBot
therefore requests 14-day windows and, if a response still looks truncated,
leaves the uncovered sessions uncached so they are retried on the next run
rather than being recorded as "no data".

Pacing
------

LumiBot enforces IB's documented historical-data limits before each request:

* no more than 55 requests per rolling 10 minutes (IB's limit is 60);
* at least 15 seconds between two identical requests;
* a small gap between back-to-back requests for the same contract.

IB pacing violations (error 162) are retried with exponential backoff. Error 200
(no security definition), 326 (client id already in use), and 354 (market data
not subscribed) fail fast with an explanatory message instead of caching an
empty result.

Failures are never silent
-------------------------

If a download chunk cannot be completed (timeout, disconnect, exhausted pacing
retries, contract error), LumiBot first writes every chunk that *did* succeed to
the cache and then raises. Nothing is silently returned as a short or empty
frame, and re-running resumes from where it stopped instead of starting over.

Only ``USD``-quoted assets are supported in v1. Passing a different quote asset
raises ``NotImplementedError`` rather than mislabeling the cached currency.

Daily bars are stamped at the session close
-------------------------------------------

IB returns daily bars dated at midnight. LumiBot re-stamps each daily bar to the
actual NYSE close for that session (16:00 ET, or 13:00 ET on an early-close day)
so a daily backtest cannot see the current session's close while that session is
still running.

Daily-cadence strategies
------------------------

When a strategy runs once a day (``sleeptime = "1D"``), ``self.get_last_price()``
for stocks reads the latest native daily bar at or before the simulated time.
Because daily bars are stamped at the session close, this never returns a close
that has not happened yet.

Portfolio valuation and order fills still use minute bars, so the first run of a
daily backtest also downloads minute history for the traded symbols. That
download is cached like any other, so later runs make no network calls.

Caching
-------

* Location: ``LUMIBOT_CACHE_FOLDER/ibkr_tws``.
* One parquet file per asset, currency, exchange, timestep, ``whatToShow``, and
  RTH setting, with a ``.meta.json`` sidecar recording the contract identity and
  cache schema version.
* Sessions that IB authoritatively reports as empty are stored as placeholder
  rows so they are never requested again.
* Pass ``force_cache_update=True`` (or delete the parquet file) to rebuild.

Known limitations
-----------------

* **Split-adjusted, not dividend-adjusted.** IB ``TRADES`` history is adjusted
  for splits but not for dividends, so total-return comparisons against
  dividend-adjusted sources will differ.
* **Survivorship bias.** IB does not serve history for delisted symbols, so a
  universe built from today's listings silently excludes failures.
* **No automatic split invalidation.** The socket API offers no free corporate
  action feed, so a split that occurs after data is cached will not rewrite the
  cache. Re-run with ``force_cache_update=True`` after a split.
* **First run is slow.** A single ``"1 M"``-sized minute request takes roughly
  10–40 seconds depending on the symbol, so the initial download of a couple of
  years across a few dozen symbols can take hours. Subsequent runs are free.
