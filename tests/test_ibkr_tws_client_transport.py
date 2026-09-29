"""Transport-level tests for :class:`lumibot.tools.ibkr_tws_helper.IBKRTWSClient`.

The end-to-end helper tests inject a fake *client*, so they never touch callback
routing, cancellation, reconnection, or the reader thread. These tests instead
drive the **real** client through a fake ``_IBApp`` that mimics ibapi's
``EClient``/``EWrapper`` contract, so the threading-sensitive code is actually
exercised. No sockets are opened.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import pytest

from lumibot.tools import ibkr_tws_helper as helper

END = dt.datetime(2024, 1, 5, 21, 1, tzinfo=dt.timezone.utc)


class FakeContract:
    symbol = "SPY"
    secType = "STK"
    exchange = "SMART"
    currency = "USD"


class FakeApp:
    """Stand-in for the lazily built ``_IBApp``.

    ``on_request`` receives ``(app, req_id)`` and decides what the gateway does:
    deliver bars, report an error, or stay silent (to exercise the timeout path).
    """

    instances: list = []

    def __init__(self, owner):
        self._owner = owner
        self._stop = threading.Event()
        self.connected = False
        self.requests = []
        self.cancelled = []
        self.connect_args = None
        self.disconnected = False
        FakeApp.instances.append(self)

    # -- behaviour hooks ----------------------------------------------------------------

    on_connect = None  # optional: (app, client_id) -> None, may report an error
    on_request = None  # optional: (app, req_id) -> None

    # -- EClient surface ----------------------------------------------------------------

    def connect(self, host, port, client_id):
        self.connect_args = (host, port, client_id)
        self.connected = True

    def run(self):
        if type(self).on_connect is not None:
            type(self).on_connect(self, self.connect_args[2])
        else:
            self._owner._on_next_valid_id(1)
        self._stop.wait()

    def disconnect(self):
        self.disconnected = True
        self.connected = False
        self._stop.set()

    def isConnected(self):  # noqa: N802 - ibapi name
        return self.connected

    def reqHistoricalData(self, req_id, contract, end, duration, bar_size, what, rth, fmt, keep, opts):  # noqa: N802
        self.requests.append(
            {"req_id": req_id, "end": end, "duration": duration, "bar_size": bar_size}
        )
        if type(self).on_request is not None:
            type(self).on_request(self, req_id)

    def cancelHistoricalData(self, req_id):  # noqa: N802
        self.cancelled.append(req_id)

    # -- convenience --------------------------------------------------------------------

    def deliver(self, req_id, bars):
        for bar in bars:
            self._owner._on_bar(req_id, bar)
        self._owner._on_end(req_id)

    def fail(self, req_id, code, message):
        self._owner._on_error(req_id, code, message)

    def drop_connection(self):
        self._owner._on_connection_closed()
        self._stop.set()


class Bar:
    def __init__(self, date="20240105  09:30:00", close=1.0):
        self.date = date
        self.open = self.high = self.low = self.close = close
        self.volume = 1


@pytest.fixture
def app_class(monkeypatch):
    FakeApp.instances = []
    FakeApp.on_connect = None
    FakeApp.on_request = None
    monkeypatch.setattr(helper, "_IB_APP_CLASS", FakeApp)
    yield FakeApp
    FakeApp.on_connect = None
    FakeApp.on_request = None


class FastClock:
    """A virtual clock so pacing waits are instant but still *happen*.

    ``PacingGuard`` loops until its wait falls to zero, so a no-op ``sleep`` that
    never advances the clock would busy-spin for the whole real cooldown.
    """

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += max(float(seconds), 0.0)


@pytest.fixture
def fast_clock(monkeypatch):
    clock = FastClock()
    monkeypatch.setattr(helper, "_pacing_guards", {})
    monkeypatch.setattr(
        helper,
        "get_pacing_guard",
        lambda host, port, budget: helper.PacingGuard(
            budget, time_func=clock.time, sleep_func=clock.sleep
        ),
    )
    return clock


@pytest.fixture
def client(app_class, fast_clock, monkeypatch):
    monkeypatch.setattr(helper.time, "sleep", lambda *_: None)
    config = helper.IBKRTWSConfig(host="127.0.0.1", port=4002, client_id=77, timeout=2.0)
    instance = helper.IBKRTWSClient(config)
    yield instance
    instance.close()


def request(client, **kwargs):
    return client.request_historical_bars(
        FakeContract(), END, "1 D", "1 min", timeout=kwargs.pop("timeout", 2.0), **kwargs
    )


# --------------------------------------------------------------------------------------
# Connection lifecycle
# --------------------------------------------------------------------------------------


def test_connects_lazily_and_only_once(client, app_class):
    app_class.on_request = lambda app, req_id: app.deliver(req_id, [Bar()])
    assert client._app is None

    request(client)
    request(client)
    assert len(app_class.instances) == 1
    assert app_class.instances[0].connect_args == ("127.0.0.1", 4002, 77)


def test_concurrent_first_requests_create_a_single_connection(client, app_class):
    app_class.on_request = lambda app, req_id: app.deliver(req_id, [Bar()])
    errors = []

    def worker():
        try:
            request(client)
        except Exception as exc:  # pragma: no cover - failure detail
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(app_class.instances) == 1, "racing callers must share one connection"


def test_client_id_collision_retries_with_the_next_id(client, app_class):
    state = {"calls": 0}

    def on_connect(app, client_id):
        state["calls"] += 1
        if state["calls"] == 1:
            app._owner._on_error(-1, 326, "Unable to connect as the client id is already in use")
        else:
            app._owner._on_next_valid_id(1)

    app_class.on_connect = staticmethod(on_connect)
    app_class.on_request = lambda app, req_id: app.deliver(req_id, [Bar()])

    request(client)
    assert client.client_id == 78
    assert [a.connect_args[2] for a in app_class.instances] == [77, 78]


def test_connection_failure_raises_with_actionable_guidance(client, app_class):
    app_class.on_connect = staticmethod(
        lambda app, client_id: app._owner._on_error(-1, 502, "Couldn't connect to TWS")
    )
    with pytest.raises(helper.IBKRTWSConnectionError) as excinfo:
        request(client)
    message = str(excinfo.value)
    assert "INTERACTIVE_BROKERS_PORT" in message
    assert "IBKR_BACKTEST_CLIENT_ID" in message


def test_dropped_connection_fails_the_waiter_and_reconnects_next_time(client, app_class):
    def first(app, req_id):
        threading.Thread(target=app.drop_connection, daemon=True).start()

    app_class.on_request = staticmethod(first)
    with pytest.raises(helper.IBKRTWSError):
        request(client)

    assert client.is_connected is False, "a closed socket must not look connected"

    app_class.on_request = staticmethod(lambda app, req_id: app.deliver(req_id, [Bar()]))
    bars = request(client)
    assert len(bars) == 1
    assert len(app_class.instances) == 2, "the next request must reconnect"


def test_close_disconnects_and_cancels_outstanding_requests(client, app_class):
    app_class.on_request = staticmethod(lambda app, req_id: app.deliver(req_id, [Bar()]))
    request(client)
    app = app_class.instances[0]
    client.close()
    assert app.disconnected is True
    client.close()  # idempotent


# --------------------------------------------------------------------------------------
# Request routing
# --------------------------------------------------------------------------------------


def test_bars_are_routed_to_the_right_concurrent_request(client, app_class):
    """Two in-flight requests must not receive each other's bars."""
    pending = {}
    gate = threading.Event()

    def on_request(app, req_id):
        pending[req_id] = app
        if len(pending) == 2:
            gate.set()

    app_class.on_request = staticmethod(on_request)
    results = {}

    def worker(tag, duration):
        # Distinct durations: identical requests are deliberately spaced 15s apart
        # by the pacing guard, which would serialize this test.
        bars = client.request_historical_bars(
            FakeContract(), END, duration, "1 min", timeout=10.0
        )
        results[tag] = [b.close for b in bars]

    threads = [
        threading.Thread(target=worker, args=("a", "1 D")),
        threading.Thread(target=worker, args=("b", "2 D")),
    ]
    for t in threads:
        t.start()
    assert gate.wait(timeout=10), "both requests should be in flight"

    req_ids = sorted(pending)
    # Deliver in reverse order, with distinct payloads, so mis-routing is visible.
    pending[req_ids[1]].deliver(req_ids[1], [Bar(close=20.0)])
    pending[req_ids[0]].deliver(req_ids[0], [Bar(close=10.0)])
    for t in threads:
        t.join(timeout=10)

    assert sorted(v[0] for v in results.values()) == [10.0, 20.0]


def test_timeout_cancels_the_request_and_raises(client, app_class):
    app_class.on_request = staticmethod(lambda app, req_id: None)  # gateway stays silent
    with pytest.raises(helper.IBKRTWSTimeoutError):
        request(client, timeout=0.2)
    app = app_class.instances[0]
    assert app.cancelled, "a timed-out request must be cancelled on the gateway"
    assert app.cancelled[0] == app.requests[0]["req_id"]


def test_late_callbacks_after_a_timeout_are_ignored(client, app_class):
    app_class.on_request = staticmethod(lambda app, req_id: None)
    with pytest.raises(helper.IBKRTWSTimeoutError):
        request(client, timeout=0.2)

    app = app_class.instances[0]
    stale_id = app.requests[0]["req_id"]
    # The gateway answers after we gave up; this must not crash or leak state.
    app.deliver(stale_id, [Bar()])
    app.fail(stale_id, 162, "HMDS query returned no data")

    app_class.on_request = staticmethod(lambda a, req_id: a.deliver(req_id, [Bar(close=5.0)]))
    bars = request(client)
    assert [b.close for b in bars] == [5.0], "stale bars must not leak into a later request"


# --------------------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------------------


def test_no_security_definition_raises_contract_error(client, app_class):
    app_class.on_request = staticmethod(
        lambda app, req_id: app.fail(req_id, 200, "No security definition has been found")
    )
    with pytest.raises(helper.IBKRTWSContractError):
        request(client)


def test_missing_subscription_raises_permission_error(client, app_class):
    app_class.on_request = staticmethod(
        lambda app, req_id: app.fail(req_id, 354, "Requested market data is not subscribed")
    )
    with pytest.raises(helper.IBKRTWSPermissionError):
        request(client)


def test_hmds_no_data_is_an_authoritative_empty_signal(client, app_class):
    app_class.on_request = staticmethod(
        lambda app, req_id: app.fail(req_id, 162, "HMDS query returned no data: SPY@SMART Trades")
    )
    with pytest.raises(helper._NoDataSignal):
        request(client)


def test_pacing_violation_retries_then_raises(client, app_class, monkeypatch):
    monkeypatch.setattr(helper.PacingGuard, "penalize", lambda self, seconds: None)
    app_class.on_request = staticmethod(
        lambda app, req_id: app.fail(req_id, 162, "Historical Market Data Service error message: pacing violation")
    )
    with pytest.raises(helper.IBKRTWSPacingViolation):
        client.request_historical_bars(
            FakeContract(), END, "1 D", "1 min", timeout=2.0, max_pacing_retries=2
        )
    assert len(app_class.instances[0].requests) == 3, "initial attempt plus two retries"


def test_pacing_violation_that_clears_returns_bars(client, app_class, monkeypatch):
    monkeypatch.setattr(helper.PacingGuard, "penalize", lambda self, seconds: None)
    state = {"n": 0}

    def on_request(app, req_id):
        state["n"] += 1
        if state["n"] == 1:
            app.fail(req_id, 162, "Historical Market Data Service error message: pacing violation")
        else:
            app.deliver(req_id, [Bar(close=7.0)])

    app_class.on_request = staticmethod(on_request)
    bars = request(client)
    assert [b.close for b in bars] == [7.0]


def test_invalid_end_datetime_retries_without_the_utc_suffix(client, app_class):
    state = {"n": 0}

    def on_request(app, req_id):
        state["n"] += 1
        if state["n"] == 1:
            app.fail(req_id, 10314, "End Date/Time: The date, time, or time-zone entered is invalid")
        else:
            app.deliver(req_id, [Bar()])

    app_class.on_request = staticmethod(on_request)
    request(client)
    sent = app_class.instances[0].requests
    assert len(sent) == 2
    assert sent[0]["end"].endswith("UTC")
    assert not sent[1]["end"].endswith("UTC")


def test_benign_status_messages_do_not_fail_a_request(client, app_class):
    def on_request(app, req_id):
        app.fail(req_id, 2106, "HMDS data farm connection is OK:ushmds")
        app.deliver(req_id, [Bar(close=3.0)])

    app_class.on_request = staticmethod(on_request)
    assert [b.close for b in request(client)] == [3.0]


def test_data_farm_outage_fails_in_flight_requests(client, app_class):
    """A degraded farm must fail the request, never quietly return zero bars."""

    def on_request(app, req_id):
        app.fail(-1, 1100, "Connectivity between IB and TWS has been lost")

    app_class.on_request = staticmethod(on_request)
    with pytest.raises(helper.IBKRTWSError):
        request(client)


# --------------------------------------------------------------------------------------
# Pacing guard
# --------------------------------------------------------------------------------------


def test_identical_requests_are_spaced_apart():
    clock = FastClock()
    guard = helper.PacingGuard(55, time_func=clock.time, sleep_func=clock.sleep)
    signature = ("SPY|STK|SMART|USD", "20240105 21:01:00 UTC", "1 D", "1 min", "TRADES", True)
    guard.acquire(signature, "SPY|STK|SMART|USD")
    guard.acquire(signature, "SPY|STK|SMART|USD")
    assert sum(clock.slept) >= helper.IDENTICAL_REQUEST_COOLDOWN - 0.01, (
        "IB rejects identical requests inside the cooldown window"
    )


def test_rolling_window_budget_is_enforced():
    clock = FastClock()
    guard = helper.PacingGuard(3, time_func=clock.time, sleep_func=clock.sleep)
    for i in range(4):
        guard.acquire((i,), f"c{i}")
    assert sum(clock.slept) >= helper.PACING_WINDOW - 1.0


def test_configured_request_budget_is_clamped_to_a_safe_ceiling():
    guard = helper.PacingGuard(500)
    assert guard.max_requests_per_10min == helper.MAX_REQUESTS_PER_10MIN_CEILING


def test_shared_guard_keeps_the_strictest_budget(monkeypatch):
    monkeypatch.setattr(helper, "_pacing_guards", {})
    first = helper.get_pacing_guard("h", 1, 55)
    second = helper.get_pacing_guard("h", 1, 10)
    assert first is second
    assert second.max_requests_per_10min == 10
