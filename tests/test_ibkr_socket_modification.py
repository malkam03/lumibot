"""Offline socket callback regressions: no TWS connection or market-data requests."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ibapi.contract import Contract
from ibapi.order import Order as NativeOrder
from ibapi.order_state import OrderState

from lumibot.brokers.interactive_brokers import (
    IBApp,
    IBClient,
    IBOrderModificationRejected,
    IBOrderModificationUnknown,
    IBWrapper,
    InteractiveBrokers,
)
from lumibot.entities import Asset, Order
from lumibot.strategies.strategy import Strategy


@pytest.fixture
def socket_order():
    """Use the real adapter/client methods with only the socket transport mocked."""
    app = IBApp.__new__(IBApp)
    IBWrapper.__init__(app)
    app.init_error()
    app.wrapper = app
    app.client_id = 17
    app.subaccount = None
    app.max_wait_time = 0.001
    app.isConnected = Mock(return_value=True)
    app.ib_broker = SimpleNamespace(on_status_event=Mock())
    native = NativeOrder()
    native.orderId = 42
    native.clientId = 17
    native.orderType = "STP LMT"
    native.action = "SELL"
    native.totalQuantity = 1
    native.auxPrice = 90
    native.lmtPrice = 89
    native.parentId = 41
    native.transmit = True
    native.tif = "GTC"
    native.outsideRth = True
    native.ocaGroup = "test-group"
    native.orderRef = "offline-test"
    native.account = "offline-paper-account"
    native.contract = Contract()
    native.contract.conId = 123
    native.contract.symbol = "SPY"
    native.contract.secType = "STK"
    native.contract.currency = "USD"
    native.orderState = OrderState()
    native.orderState.status = "Submitted"
    app.get_open_orders = Mock(return_value=[native])
    app.placeOrder = Mock()
    broker = InteractiveBrokers.__new__(InteractiveBrokers)
    broker.ib = app
    order = Order("offline", Asset("SPY"), 1, "sell", stop_price=90, stop_limit_price=89)
    order.identifier = 42
    order.status = "new"
    return app, broker, order, native


def acknowledge(app, native, status="Submitted"):
    state = OrderState()
    state.status = status
    app.openOrder(native.orderId, native.contract, native, state)


@pytest.mark.parametrize("status", ["Submitted", "PreSubmitted"])
def test_stop_limit_success_preserves_every_native_field(socket_order, status):
    app, broker, order, native = socket_order
    before = deepcopy(native.__dict__)

    def send(order_id, contract, updated):
        assert order_id == native.orderId
        assert order.stop_price == 90
        assert order.stop_limit_price == 89
        assert contract.conId == 123
        acknowledge(app, updated, status)

    app.placeOrder.side_effect = send
    assert broker.modify_order(order, stop_price=92, limit_price=91) is order
    assert order.stop_price == 92
    assert order.stop_limit_price == order.limit_price == 91
    sent = app.placeOrder.call_args.args[2]
    for name in before.keys() - {"auxPrice", "lmtPrice", "contract", "orderState"}:
        actual = getattr(sent, name)
        expected = before[name]
        if hasattr(actual, "__dict__"):
            assert actual.__dict__ == expected.__dict__, name
        else:
            assert actual == expected, name
    assert native.auxPrice == 90 and native.lmtPrice == 89
    assert order._raw.parentId == 41
    assert order._raw.transmit is True
    assert order._raw.contract.conId == 123
    app.placeOrder.assert_called_once()


@pytest.mark.parametrize("kind,limit,stop", [("LMT", 91, None), ("STP", None, 92)])
def test_single_price_modification(socket_order, kind, limit, stop):
    app, broker, order, native = socket_order
    native.orderType = kind
    app.placeOrder.side_effect = lambda _, contract, updated: acknowledge(app, updated)
    assert broker.modify_order(order, limit_price=limit, stop_price=stop) is order
    sent = app.placeOrder.call_args.args[2]
    assert sent.auxPrice == (90 if stop is None else stop)
    assert sent.lmtPrice == (89 if limit is None else limit)


def test_rejection_propagates_through_strategy_without_mutation(socket_order):
    app, broker, order, _ = socket_order
    app.placeOrder.side_effect = lambda *_: app.error(42, 201, "offline rejection")
    strategy = SimpleNamespace(broker=broker)
    with pytest.raises(IBOrderModificationRejected) as caught:
        Strategy.modify_order(strategy, order, limit_price=91, stop_price=92)
    assert caught.value.order_id == 42
    assert caught.value.error_code == 201
    assert order.stop_price == 90 and order.stop_limit_price == 89
    assert order.limit_price is None
    assert order._raw is None


@pytest.mark.parametrize("response", ["silence", "old-price", "status-only", "warning", "unclassified", "disconnect", "send-error"])
def test_unknown_never_changes_prices_and_blocks_blind_retry(socket_order, response):
    app, broker, order, original = socket_order

    def send(*_):
        if response == "old-price":
            acknowledge(app, original)
        elif response == "status-only":
            app.orderStatus(42, "Submitted", 0, 1, 0, 1, 41, 0, 17, "", 0)
        elif response == "warning":
            app.error(42, 399, "offline warning")
        elif response == "unclassified":
            app.error(42, 999, "offline unknown error")
        elif response == "disconnect":
            app.connectionClosed()
        elif response == "send-error":
            raise OSError("offline send failed")

    app.placeOrder.side_effect = send
    with pytest.raises(IBOrderModificationUnknown):
        Strategy.modify_order(SimpleNamespace(broker=broker), order, stop_price=92)
    assert order.stop_price == 90 and order.stop_limit_price == 89
    assert order._raw is None
    # Even a late acknowledgement must not silently unfreeze an uncertain caller.
    acknowledge(app, app.placeOrder.call_args.args[2])
    with pytest.raises(IBOrderModificationUnknown):
        broker.modify_order(order, stop_price=93)
    app.placeOrder.assert_called_once()


@pytest.mark.parametrize("field,value", [
    ("clientId", 18), ("parentId", 99), ("transmit", False), ("totalQuantity", 2),
    ("orderType", "STP"), ("action", "BUY"), ("account", "different-account"),
])
def test_mismatched_callback_cannot_acknowledge(socket_order, field, value):
    app, broker, order, _ = socket_order

    def send(_, contract, updated):
        response = deepcopy(updated)
        setattr(response, field, value)
        acknowledge(app, response)

    app.placeOrder.side_effect = send
    with pytest.raises(IBOrderModificationUnknown):
        broker.modify_order(order, stop_price=92)


@pytest.mark.parametrize("status", ["Filled", "Cancelled", "ApiCancelled", "Inactive"])
def test_terminal_callback_is_unknown_not_price_success(socket_order, status):
    app, broker, order, _ = socket_order
    app.placeOrder.side_effect = lambda *_: app.orderStatus(42, status, 0, 1, 0, 1, 41, 0, 17, "", 0)
    with pytest.raises(IBOrderModificationUnknown):
        broker.modify_order(order, stop_price=92)
    assert order.stop_price == 90


@pytest.mark.parametrize("status", ["filled", "canceled", "error", "unknown"])
def test_inactive_local_order_rejected_without_transport(socket_order, status):
    app, broker, order, _ = socket_order
    order.status = status
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    app.placeOrder.assert_not_called()
    app.get_open_orders.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("clientId", 18), ("orderType", "TRAIL"),
])
def test_foreign_or_unsupported_native_order_is_not_adopted(socket_order, field, value):
    app, broker, order, native = socket_order
    setattr(native, field, value)
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    app.placeOrder.assert_not_called()


@pytest.mark.parametrize("status", ["PendingSubmit", "PendingCancel", "Inactive", "Filled"])
def test_inactive_native_order_is_not_sent(socket_order, status):
    app, broker, order, native = socket_order
    native.orderState.status = status
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    app.placeOrder.assert_not_called()


def test_account_mismatch_and_missing_order_are_not_adopted(socket_order):
    app, broker, order, _ = socket_order
    app.subaccount = "configured-test-account"
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    app.subaccount = None
    app.get_open_orders.return_value = []
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    app.placeOrder.assert_not_called()


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf"), True])
def test_invalid_price_rejected_before_snapshot(socket_order, price):
    app, broker, order, _ = socket_order
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=price)
    app.get_open_orders.assert_not_called()


def test_noop_is_confirmed_by_snapshot_without_send(socket_order):
    app, broker, order, _ = socket_order
    assert broker.modify_order(order, stop_price=90, limit_price=89) is order
    assert order.stop_limit_price == order.limit_price == 89
    app.placeOrder.assert_not_called()


def test_snapshot_failure_and_disconnection_are_explicit(socket_order):
    app, broker, order, _ = socket_order
    app.get_open_orders.return_value = None
    with pytest.raises(ConnectionError):
        broker.modify_order(order, stop_price=92)
    app.isConnected.return_value = False
    with pytest.raises(ConnectionError):
        broker.modify_order(order, stop_price=92)
    app.placeOrder.assert_not_called()


def test_missing_account_and_asset_mismatch_fail_closed(socket_order):
    app, broker, order, native = socket_order
    native.account = ""
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    native.account = "offline-paper-account"
    native.contract.symbol = "IWM"
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    app.placeOrder.assert_not_called()


def test_local_foreign_client_metadata_rejected_before_snapshot(socket_order):
    app, broker, order, _ = socket_order
    order.broker_client_id = 18
    with pytest.raises(ValueError, match="foreign"):
        broker.modify_order(order, stop_price=92)
    app.get_open_orders.assert_not_called()
    app.placeOrder.assert_not_called()


@pytest.mark.parametrize("metadata,value", [
    ("broker_perm_id", 999), ("broker_account", "other-offline-account"),
])
def test_conflicting_local_permanent_identity_cannot_modify(socket_order, metadata, value):
    app, broker, order, _ = socket_order
    setattr(order, metadata, value)
    with pytest.raises(ValueError, match="identity/account"):
        broker.modify_order(order, stop_price=92)
    app.placeOrder.assert_not_called()


def test_foreign_status_does_not_finish_own_modification(socket_order):
    app, broker, order, _ = socket_order

    def send(_, contract, updated):
        app.orderStatus(42, "Filled", 1, 0, 100, 1, 41, 100, 18, "", 0)
        acknowledge(app, updated)

    app.placeOrder.side_effect = send
    assert broker.modify_order(order, stop_price=92) is order


def test_duplicate_id_rejection_never_uses_submission_retry(socket_order):
    app, broker, order, _ = socket_order
    app.reqIds = Mock(side_effect=AssertionError("Modification must not allocate a new ID"))
    app.placeOrder.side_effect = lambda *_: app.error(42, 103, "offline duplicate ID")
    with pytest.raises(IBOrderModificationRejected) as caught:
        broker.modify_order(order, stop_price=92)
    assert caught.value.error_code == 103
    assert app._pending_submissions == {}
    assert app._rejected_order_ids == set()
    app.placeOrder.assert_called_once()
    app.reqIds.assert_not_called()


def test_option_identity_includes_expiry_strike_and_right(socket_order):
    import datetime

    app, broker, order, native = socket_order
    order.asset = Asset("SPY", asset_type="option", expiration=datetime.date(2026, 11, 20), strike=100, right="CALL")
    native.contract.secType = "OPT"
    native.contract.lastTradeDateOrContractMonth = "20261120"
    native.contract.right = "C"
    native.contract.strike = 101
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    native.contract.strike = 100
    native.contract.lastTradeDateOrContractMonth = "20261121"
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    native.contract.lastTradeDateOrContractMonth = "20261120"
    app.placeOrder.side_effect = lambda _, contract, updated: acknowledge(app, updated)
    assert broker.modify_order(order, stop_price=92) is order


@pytest.mark.parametrize("kind,kwargs", [
    ("LMT", {"stop_price": 92}), ("STP", {"limit_price": 91}),
])
def test_price_field_incompatible_with_native_type(socket_order, kind, kwargs):
    app, broker, order, native = socket_order
    native.orderType = kind
    with pytest.raises(ValueError):
        broker.modify_order(order, **kwargs)
    app.placeOrder.assert_not_called()


def test_invalid_identifier_and_empty_request(socket_order):
    app, broker, order, _ = socket_order
    with pytest.raises(ValueError):
        broker.modify_order(order)
    order.identifier = "42"
    with pytest.raises(ValueError):
        broker.modify_order(order, stop_price=92)
    app.get_open_orders.assert_not_called()


def test_socket_thread_cannot_block_itself(socket_order):
    from threading import current_thread

    app, broker, order, _ = socket_order
    app._thread = current_thread()
    with pytest.raises(RuntimeError, match="callback thread"):
        broker.modify_order(order, stop_price=92)
    app.placeOrder.assert_not_called()


def test_inactive_status_followed_by_explicit_rejection(socket_order):
    app, broker, order, _ = socket_order

    def reject(*_):
        app.orderStatus(42, "Inactive", 0, 1, 0, 1, 41, 0, 17, "", 0)
        app.error(42, 201, "offline rejection")

    app.placeOrder.side_effect = reject
    with pytest.raises(IBOrderModificationRejected):
        broker.modify_order(order, stop_price=92)


def test_contract_mismatch_cannot_confirm(socket_order):
    app, broker, order, _ = socket_order

    # Change the callback contract, not the expected native snapshot.
    def send(_, contract, updated):
        response = deepcopy(updated)
        response.contract.conId = 999
        acknowledge(app, response)

    app.placeOrder.side_effect = send
    with pytest.raises(IBOrderModificationUnknown):
        broker.modify_order(order, stop_price=92)


def test_real_snapshot_callbacks_feed_existing_queue(socket_order):
    app, _, _, native = socket_order
    del app.get_open_orders

    def snapshot():
        acknowledge(app, deepcopy(native))
        app.openOrderEnd()

    app.reqAllOpenOrders = Mock(side_effect=snapshot)
    assert IBClient.get_open_orders(app)[0].orderId == 42
    app.reqAllOpenOrders.assert_called_once()
