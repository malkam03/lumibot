"""Offline tests of the opt-in probe, including its exact native OTO topology."""

import datetime as dt
from copy import deepcopy
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock

import pytest
from ibapi.order_state import OrderState

from lumibot.brokers.interactive_brokers import IBOrderModificationUnknown, IBWrapper
from scripts import ibkr_paper_modification_probe as probe


class OfflinePaper(probe.PaperProbeApp):
    """Run real adapter modifications with deterministic in-memory IB callbacks."""

    def __init__(self):
        IBWrapper.__init__(self)
        self.init_error()
        self.wrapper = self
        self.client_id, self.subaccount, self.max_wait_time = 17, None, 0.001
        self.managed_accounts = ["DU123456"]
        self.ib_broker = SimpleNamespace(on_status_event=Mock())
        self.nextValidOrderId = 50
        self.native = {}
        self.sent = []
        self.holding = 0
        self.executed = {}
        self.ignore_modifications = False

    def isConnected(self):
        return True

    def get_positions(self):
        return [{"symbol": "SPY", "position": self.holding}]

    def get_open_orders(self):
        return [deepcopy(native) for native in self.native.values()]

    def quote_prices(self, symbol):
        return 100, 100.01

    def placeOrder(self, order_id, contract, native):
        self.sent.append(deepcopy(native))
        modification = order_id in self.native
        if modification and self.ignore_modifications:
            return
        response = deepcopy(native)
        response.clientId = self.client_id
        response.contract = deepcopy(contract)
        response.orderState = OrderState()
        response.orderState.status = "Submitted"
        self.openOrder(order_id, response.contract, response, response.orderState)
        self.native[order_id] = response
        if native.parentId and not modification:
            self.holding = 1
            self.executed[native.parentId] = (1, "BOT")
            del self.native[native.parentId]
        if modification and native.auxPrice > 100.01:
            self.holding = 0
            self.executed[order_id] = (1, "SLD")
            del self.native[order_id]


@pytest.fixture(autouse=True)
def forbid_connections(monkeypatch):
    """Any accidental real connection immediately fails these tests."""
    monkeypatch.setattr(probe.IBClient, "connect", Mock(side_effect=AssertionError("No socket in offline tests")))


def test_default_cli_never_constructs_or_connects_probe(monkeypatch, capsys):
    constructor = Mock(side_effect=AssertionError("Default must not construct an app"))
    monkeypatch.setattr(probe, "PaperProbeApp", constructor)
    assert probe.main([]) == 0
    constructor.assert_not_called()
    assert "NO ACTION" in capsys.readouterr().out


@pytest.mark.parametrize("accounts", [[], ["U123456"], ["DU123456", "U654321"], ["DU1", "DU2"], ["DU"], ["DUabc"]])
def test_nonexclusive_paper_identity_fails_closed(accounts):
    with pytest.raises(RuntimeError):
        probe.require_paper_account(accounts)


def test_paper_identity_is_verified_not_inferred_from_port():
    assert probe.require_paper_account(["DU123456"]) == "DU123456"


def test_symbol_selection_avoids_any_existing_exposure_or_order():
    positions = [{"symbol": "SPY", "position": 1}]
    orders = [SimpleNamespace(contract=SimpleNamespace(symbol="IWM"))]
    assert probe.unused_symbol(["SPY", "IWM", "DIA"], positions, orders) == "DIA"
    with pytest.raises(RuntimeError):
        probe.unused_symbol(["SPY", "IWM"], positions, orders)
    with pytest.raises(RuntimeError):
        probe.unused_symbol(["SPY"], None, [])


@pytest.mark.parametrize("now", [
    dt.datetime(2026, 10, 29, 19, 50, tzinfo=dt.timezone.utc),
    dt.datetime(2026, 10, 31, 16, tzinfo=dt.timezone.utc),
    dt.datetime(2026, 11, 27, 17, 50, tzinfo=dt.timezone.utc),
])
def test_late_session_weekend_and_early_close_are_blocked(now):
    with pytest.raises(RuntimeError):
        probe.require_trading_window(now)


def test_regular_session_window_is_accepted():
    probe.require_trading_window(dt.datetime(2026, 10, 29, 15, tzinfo=dt.timezone.utc))


def test_one_share_oto_tighten_and_exit_retain_same_id(monkeypatch, capsys):
    monkeypatch.setattr(probe, "require_trading_window", lambda: None)
    app = OfflinePaper()
    result = probe.run_probe(app, ["SPY"], timeout=0.01)
    parent, child, tighter, exit_order = app.sent
    assert parent.totalQuantity == child.totalQuantity == 1
    assert parent.transmit is False
    assert child.orderType == "STP LMT" and child.transmit is True
    assert child.parentId == tighter.parentId == exit_order.parentId == parent.orderId
    assert child.orderId == tighter.orderId == exit_order.orderId == result["stop_id"]
    assert child.orderRef == tighter.orderRef == exit_order.orderRef == parent.orderRef
    assert child.auxPrice < tighter.auxPrice < exit_order.auxPrice
    assert tighter.lmtPrice < tighter.auxPrice
    assert exit_order.lmtPrice < 100
    assert app.holding == 0 and not app.native
    assert result["flat"] is True
    output = capsys.readouterr().out
    assert "PASS" in output and "DU123456" not in output


def test_unknown_stops_probe_without_replacement_or_cancel(monkeypatch):
    monkeypatch.setattr(probe, "require_trading_window", lambda: None)
    app = OfflinePaper()
    app.ignore_modifications = True
    with pytest.raises(IBOrderModificationUnknown):
        probe.run_probe(app, ["SPY"], timeout=0.01)
    assert len(app.sent) == 3  # parent, child, one tightening attempt; never another SELL
    assert app.holding == 1
    assert list(app.native) == [51]  # protective stop remains untouched


def test_entry_timeout_never_advances_to_modification(monkeypatch):
    monkeypatch.setattr(probe, "require_trading_window", lambda: None)
    app = OfflinePaper()
    app.filled = lambda _: False
    with pytest.raises(TimeoutError):
        probe.run_probe(app, ["SPY"], timeout=0.001)
    assert len(app.sent) == 2


@pytest.mark.parametrize("bid,ask", [(0, 100), (100, float("nan")), (100, 99), (100, 102)])
def test_bad_quotes_block_submission(monkeypatch, bid, ask):
    monkeypatch.setattr(probe, "require_trading_window", lambda: None)
    app = OfflinePaper()
    app.quote_prices = lambda _: (bid, ask)
    with pytest.raises(RuntimeError):
        probe.run_probe(app, ["SPY"], timeout=0.01)
    assert not app.sent


def test_notional_cap_blocks_submission():
    app = OfflinePaper()
    with pytest.raises(RuntimeError):
        probe.run_probe(app, ["SPY"], max_notional=50)
    assert not app.sent


def test_second_exposure_check_blocks_racing_strategy(monkeypatch):
    monkeypatch.setattr(probe, "require_trading_window", lambda: None)
    app = OfflinePaper()
    app.get_positions = Mock(side_effect=[[], [{"symbol": "SPY", "position": 1}]])
    with pytest.raises(RuntimeError):
        probe.run_probe(app, ["SPY"])
    assert not app.sent


def test_failure_output_is_sanitized_and_disconnects(monkeypatch, capsys):
    monkeypatch.setattr(probe, "require_trading_window", lambda: None)
    app = Mock()
    app.await_ready.side_effect = RuntimeError("sensitive-placeholder-not-for-output")
    monkeypatch.setattr(probe, "PaperProbeApp", Mock(return_value=app))
    assert probe.main(["--execute-paper", "--client-id", "17"]) == 1
    app.disconnect.assert_called_once()
    app._thread.join.assert_called_once()
    assert "sensitive-placeholder" not in capsys.readouterr().err


@pytest.mark.parametrize("market_type", [1, 2, 3, 4])
def test_quote_transport_requires_realtime_type_and_cancels_subscription(market_type):
    app = probe.PaperProbeApp.__new__(probe.PaperProbeApp)
    app._quote_ready = Event()
    app.max_wait_time = 0.001
    app.get_reqid = lambda: 10001
    app.reqMarketDataType = Mock()
    app.cancelMktData = Mock()

    def ticks(request_id, *_):
        app.marketDataType(request_id, market_type)
        app.tickPrice(request_id, 1, 100, None)
        app.tickPrice(request_id, 2, 100.01, None)

    app.reqMktData = Mock(side_effect=ticks)
    if market_type == 1:
        assert app.quote_prices("SPY") == (100, 100.01)
    else:
        with pytest.raises(TimeoutError):
            app.quote_prices("SPY")
    app.cancelMktData.assert_called_once_with(10001)


def test_execution_evidence_ignores_foreign_client_or_account():
    app = probe.PaperProbeApp.__new__(probe.PaperProbeApp)
    app.managed_accounts = ["DU123456"]
    app.client_id, app.executed = 17, {}
    execution = SimpleNamespace(acctNumber="DU123456", clientId=18, orderId=50, cumQty=1, side="BOT")
    app.execDetails(0, None, execution)
    assert not app.filled(50)
    execution.clientId, execution.acctNumber = 17, "DU654321"
    app.execDetails(0, None, execution)
    assert not app.filled(50)
    execution.acctNumber = "DU123456"
    app.execDetails(0, None, execution)
    assert app.filled(50)


def test_handshake_timeout_is_bounded():
    app = probe.PaperProbeApp.__new__(probe.PaperProbeApp)
    app.ready, app.accounts_ready = Event(), Event()
    app.max_wait_time = 0.001
    with pytest.raises(TimeoutError):
        app.await_ready()
