"""Network-free regressions for independent TWS API clients sharing an account."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event, RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ibapi.contract import Contract
from ibapi.order import Order as NativeOrder

from lumibot.brokers.broker import Broker
from lumibot.brokers.interactive_brokers import IBApp, IBWrapper, InteractiveBrokers
from lumibot.entities import Asset, Order
from lumibot.strategies.strategy_executor import StrategyExecutor
from lumibot.trading_builtins import SafeList


def response(client=7, identifier=100):
    contract = Contract()
    contract.symbol = contract.localSymbol = "BBB"
    contract.secType = "STK"
    contract.currency = "USD"
    return SimpleNamespace(
        contract=contract, totalQuantity=11, lmtPrice=210.92, auxPrice=0,
        tif="DAY", goodTillDate="", action="BUY", orderId=identifier,
        clientId=client, permId=2002, account="",
        orderState=SimpleNamespace(status="Submitted"),
    )


@pytest.fixture
def broker():
    result = object.__new__(InteractiveBrokers)
    result.client_id = 7
    result.subaccount = None
    result.ib = SimpleNamespace(get_open_orders=lambda: [response(22), response()])
    return result


def test_snapshot_and_lookup_only_return_own_client(broker):
    assert [item.clientId for item in broker._pull_broker_all_orders()] == [7]
    assert broker._pull_broker_order(100).clientId == 7
    broker.ib.get_open_orders = lambda: [response(22)]
    assert broker._pull_broker_order(100) is None


def test_parser_rejects_foreign_client_and_preserves_identity(broker):
    assert broker._parse_broker_order(response(22), "example") is None
    parsed = broker._parse_broker_order(response(), "example")
    assert parsed.broker_client_id == 7
    assert parsed.broker_perm_id == 2002
    assert parsed.broker_account == ""


def test_foreign_order_cannot_resurrect_terminal_order(broker):
    original = Order("example", Asset("AAA"), 35, "buy", identifier=100)
    original.status = Order.OrderStatus.FILLED
    broker.ib.get_open_orders = lambda: [response(22)]
    broker.get_all_orders = lambda: [original]
    broker._invalidate_order_caches = Mock()
    broker.sync_orders("example")
    assert original.status == Order.OrderStatus.FILLED
    assert original.quantity == 35
    assert original.limit_price is None


def test_foreign_callbacks_and_cancel_are_rejected(broker):
    broker.get_tracked_order = Mock()
    broker.order_status_duplicates = []
    broker.on_status_event(100, "Submitted", 0, 11, 0, 2002, 0, 0, 22, "", 0)
    broker.on_trade_event(0, None, SimpleNamespace(clientId=22, orderId=100))
    broker.get_tracked_order.assert_not_called()
    foreign = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    foreign.broker_client_id = 22
    broker.ib.cancel_order = Mock()
    with pytest.raises(ValueError, match="foreign"):
        broker.cancel_order(foreign)
    broker.ib.cancel_order.assert_not_called()


def app_with_ids():
    app = object.__new__(IBApp)
    IBWrapper.__init__(app)
    app.nextValidId(10)  # Connection-time value must not be used as a fresh reply.
    app.reqIds = Mock(side_effect=lambda _: app.nextValidId(100))
    return app


def test_each_allocation_requests_fresh_id_and_remains_monotonic():
    app = app_with_ids()
    with ThreadPoolExecutor(max_workers=4) as pool:
        allocated = list(pool.map(lambda _: app.nextOrderId(), range(8)))
    assert sorted(allocated) == list(range(100, 108))
    assert app.reqIds.call_count == 8


def test_fresh_id_timeout_does_not_use_cached_value():
    app = app_with_ids()
    app.reqIds = Mock()
    with pytest.raises(TimeoutError, match="nextValidId"):
        app.nextOrderId(timeout=0.01)


def test_allocation_exceeds_observed_foreign_order_ids():
    app = app_with_ids()
    app.client_id = 7
    foreign = NativeOrder()
    foreign.clientId = 22
    app.openOrder(900, Contract(), foreign, SimpleNamespace(status="Submitted"))
    assert app.nextOrderId() == 901


@pytest.mark.parametrize("codes,expected_count", [([103, None], 2), ([103, 103], 2), ([201], 1)])
def test_submission_retries_only_duplicate_id_once(codes, expected_count):
    app = app_with_ids()
    app.client_id = 7
    app.subaccount = None
    app.wrapper = app
    app.ib_broker = SimpleNamespace(
        _unprocessed_orders=SafeList(None), _invalidate_order_caches=Mock(),
        _process_trade_event=Mock(), ERROR_ORDER="error",
    )
    order = Order(
        "example", Asset("BBB"), 1, "buy",
        quote=Asset("USD", asset_type=Asset.AssetType.FOREX),
    )
    app.create_contract = Mock(return_value=Contract())
    sent = []

    def place(identifier, contract, native):
        sent.append(identifier)
        code = codes[len(sent) - 1]
        if code:
            app.error(identifier, code, "synthetic rejection")
        else:
            app.openOrder(identifier, contract, native, SimpleNamespace(status="Submitted"))

    app.placeOrder = Mock(side_effect=place)
    if codes[-1] is not None:
        with pytest.raises(RuntimeError, match="rejected"):
            app.execute_order(order)
    else:
        app.execute_order(order)
        assert order.identifier == sent[-1]
        assert order.broker_client_id == 7
    assert len(sent) == expected_count
    assert len(set(sent)) == len(sent)
    assert app.ib_broker._process_trade_event.call_count == (codes[-1] is not None)


@pytest.mark.parametrize("executor_sync", [False, True])
def test_both_sync_paths_ignore_foreign_and_update_only_matching_identity(broker, executor_sync):
    own = broker._parse_broker_order(response(), "example")
    own.quantity = 35
    own.status = Order.OrderStatus.PARTIALLY_FILLED
    terminal = Order("example", Asset("AAA"), 35, "buy", identifier=101)
    terminal.status = Order.OrderStatus.FILLED
    foreign_local = Order("example", Asset("BBB"), 2, "buy", identifier=102)
    foreign_local.broker_client_id = 22
    broker.ib.get_open_orders = lambda: [response(22, 101), response(22, 103), response()]
    broker.get_all_orders = lambda: [terminal, foreign_local, own]
    broker._invalidate_order_caches = Mock()
    broker._process_broker_synced_order = Mock()
    broker._process_new_order = Mock()
    broker._refresh_missing_active_order_from_broker = Mock()
    broker._orders_queue = Queue()
    broker._held_trades = []
    broker.process_held_trades = Mock()
    broker.sync_positions = Mock()
    broker._get_balances_at_broker = Mock(return_value=(100, {}, 100))
    if executor_sync:
        executor = object.__new__(StrategyExecutor)
        executor.broker = broker
        executor.strategy = SimpleNamespace(
            name="example", _name="example", logger=Mock(), quote_asset=None, _set_cash_position=Mock(),
        )
        executor.sync_broker()
        assert broker._hold_trade_events is False
    else:
        broker.sync_orders("example")
    assert terminal.status == Order.OrderStatus.FILLED
    assert terminal.quantity == 35
    assert foreign_local.quantity == 2
    assert own.quantity == 11
    assert own.broker_perm_id == 2002
    broker._process_broker_synced_order.assert_not_called()
    broker._process_new_order.assert_not_called()
    broker._refresh_missing_active_order_from_broker.assert_not_called()


@pytest.mark.parametrize("conflict", ["asset", "broker_perm_id", "broker_account"])
def test_same_client_conflicting_identity_is_not_merged(broker, conflict):
    original = broker._parse_broker_order(response(), "example")
    original.status = Order.OrderStatus.FILLED
    if conflict == "asset":
        original.asset = Asset("AAA")
    else:
        setattr(original, conflict, 9999 if conflict == "broker_perm_id" else "synthetic-other")
    incoming = response()
    incoming.account = "synthetic-account"
    broker.ib.get_open_orders = lambda: [incoming]
    broker.get_all_orders = lambda: [original]
    broker._invalidate_order_caches = Mock()
    broker._process_broker_synced_order = Mock()
    broker.sync_orders("example")
    assert original.status == Order.OrderStatus.FILLED
    broker._process_broker_synced_order.assert_not_called()


def test_empty_remote_metadata_does_not_erase_known_permanent_identity(broker):
    original = broker._parse_broker_order(response(), "example")
    incoming = broker._parse_broker_order(response(), "example")
    incoming.broker_perm_id = 0
    broker._record_order_identity(original, incoming)
    assert original.broker_perm_id == 2002


@pytest.mark.parametrize(
    "asset,sec_type,contract_symbol,local_symbol,last_trade_date,strike,right,multiplier",
    [
        (
            Asset("BBB", asset_type=Asset.AssetType.OPTION, expiration="2026-11-20", strike=600, right="CALL"),
            "OPT", "BBB", "BBB   261120C00600000", "20261120", 600, "C", "100",
        ),
        (
            Asset("ES", asset_type=Asset.AssetType.FUTURE, expiration="2026-12-18"),
            "FUT", "ES", "ESZ6", "20261218", 0, "", "50",
        ),
    ],
)
def test_parsed_derivative_order_matches_the_submitted_root_contract(
    broker, asset, sec_type, contract_symbol, local_symbol, last_trade_date, strike, right, multiplier,
):
    remote = response()
    remote.contract.symbol = contract_symbol
    remote.contract.localSymbol = local_symbol
    remote.contract.secType = sec_type
    remote.contract.lastTradeDateOrContractMonth = last_trade_date
    remote.contract.strike = strike
    remote.contract.right = right
    remote.contract.multiplier = multiplier
    parsed = broker._parse_broker_order(remote, "example")
    local = Order("example", asset, 11, "buy", identifier=100)
    local.broker_client_id = 7
    local.broker_perm_id = 2002

    assert parsed.asset == asset
    assert broker._orders_match(local, parsed)


def test_derivative_contract_mismatch_still_rejects_order_match(broker):
    remote = response()
    remote.contract.symbol = "BBB"
    remote.contract.localSymbol = "BBB   261120C00600000"
    remote.contract.secType = "OPT"
    remote.contract.lastTradeDateOrContractMonth = "20261120"
    remote.contract.strike = 600
    remote.contract.right = "C"
    remote.contract.multiplier = "100"
    parsed = broker._parse_broker_order(remote, "example")
    different_contract = Order(
        "example",
        Asset("BBB", asset_type=Asset.AssetType.OPTION, expiration="2026-12-18", strike=600, right="CALL"),
        11,
        "buy",
        identifier=100,
    )
    different_contract.broker_client_id = 7
    different_contract.broker_perm_id = 2002

    assert not broker._orders_match(different_contract, parsed)


def test_duplicate_cleanup_mutates_only_verified_order_identities(broker):
    remote = broker._parse_broker_order(response(), "example")
    valid_first = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    valid_second = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    conflict_asset = Order("example", Asset("AAA"), 11, "buy", identifier=100)
    conflict_perm_id = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    foreign = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    for order in (valid_first, valid_second, conflict_asset, conflict_perm_id):
        order.broker_client_id = 7
        order.broker_perm_id = 2002
    conflict_perm_id.broker_perm_id = 9999
    foreign.broker_client_id = 22
    foreign.broker_perm_id = 2002
    conflict_asset.status = Order.OrderStatus.FILLED
    conflict_perm_id.status = Order.OrderStatus.CANCELED
    foreign.status = Order.OrderStatus.FILLED

    buckets = {}
    for name in (
        "_unprocessed_orders", "_new_orders", "_partially_filled_orders",
        "_filled_orders", "_error_orders", "_canceled_orders", "_placeholder_orders",
    ):
        buckets[name] = SafeList(None)
        setattr(broker, name, buckets[name])
    broker._lock = RLock()
    broker._invalidate_order_caches = Mock()
    buckets["_unprocessed_orders"].append(foreign)
    buckets["_new_orders"].append(valid_first)
    buckets["_new_orders"].append(conflict_asset)
    buckets["_filled_orders"].append(valid_second)
    buckets["_canceled_orders"].append(conflict_perm_id)

    survivor = broker._clean_order_trackers(remote)

    assert survivor is valid_first
    assert any(order is foreign for order in buckets["_unprocessed_orders"].get_list())
    assert any(order is conflict_asset for order in buckets["_new_orders"].get_list())
    assert any(order is conflict_perm_id for order in buckets["_canceled_orders"].get_list())
    assert valid_second not in buckets["_filled_orders"].get_list()
    assert conflict_asset.status == Order.OrderStatus.FILLED
    assert conflict_perm_id.status == Order.OrderStatus.CANCELED
    assert foreign.status == Order.OrderStatus.FILLED


@pytest.mark.parametrize("executor_sync", [False, True])
def test_both_sync_paths_cleanup_only_verified_duplicate_identities(broker, executor_sync):
    remote = broker._parse_broker_order(response(), "example")
    valid_first = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    valid_second = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    conflict_asset = Order("example", Asset("AAA"), 11, "buy", identifier=100)
    conflict_perm_id = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    foreign = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    for order in (valid_first, valid_second, conflict_asset, conflict_perm_id):
        order.broker_client_id = 7
        order.broker_perm_id = 2002
    conflict_perm_id.broker_perm_id = 9999
    foreign.broker_client_id = 22
    foreign.broker_perm_id = 2002
    conflict_asset.status = Order.OrderStatus.FILLED
    conflict_perm_id.status = Order.OrderStatus.CANCELED
    foreign.status = Order.OrderStatus.FILLED

    for name in (
        "_unprocessed_orders", "_new_orders", "_partially_filled_orders",
        "_filled_orders", "_error_orders", "_canceled_orders", "_placeholder_orders",
    ):
        setattr(broker, name, SafeList(None))
    broker._unprocessed_orders.append(foreign)
    broker._new_orders.append(valid_first)
    broker._new_orders.append(conflict_asset)
    broker._filled_orders.append(valid_second)
    broker._canceled_orders.append(conflict_perm_id)
    broker._lock = RLock()
    broker._first_iteration = False
    broker.IS_BACKTESTING_BROKER = False
    broker._pull_all_orders = Mock(return_value=[remote])
    broker.get_all_orders = lambda: [valid_first, valid_second, conflict_asset, conflict_perm_id, foreign]
    broker._invalidate_order_caches = Mock()

    if executor_sync:
        broker._orders_queue = Queue()
        broker._held_trades = []
        broker.process_held_trades = Mock()
        broker.sync_positions = Mock()
        broker._get_balances_at_broker = Mock(return_value=(100, {}, 100))
        broker._process_broker_synced_order = Mock()
        broker._process_new_order = Mock()
        broker._refresh_missing_active_order_from_broker = Mock()
        executor = object.__new__(StrategyExecutor)
        executor.broker = broker
        executor.strategy = SimpleNamespace(
            name="example",
            _name="example",
            logger=Mock(),
            quote_asset=None,
            _set_cash_position=Mock(),
        )
        executor.sync_broker()
    else:
        broker.sync_orders("example")

    assert any(order is valid_first for order in broker._new_orders.get_list())
    assert not any(order is valid_second for order in broker._filled_orders.get_list())
    assert any(order is foreign for order in broker._unprocessed_orders.get_list())
    assert any(order is conflict_asset for order in broker._new_orders.get_list())
    assert any(order is conflict_perm_id for order in broker._canceled_orders.get_list())
    assert conflict_asset.status == Order.OrderStatus.FILLED
    assert conflict_perm_id.status == Order.OrderStatus.CANCELED
    assert foreign.status == Order.OrderStatus.FILLED


@pytest.mark.parametrize("status", [Order.OrderStatus.FILLED, Order.OrderStatus.PARTIALLY_FILLED])
def test_fast_fill_callback_is_not_reset_to_submitted(broker, status):
    order = Order("example", Asset("BBB"), 1, "buy")
    broker.ib.execute_order = Mock(side_effect=lambda submitted: setattr(submitted, "status", status))
    broker._submit_order(order)
    assert order.status == status


def test_missing_direct_lookup_preserves_foreign_local_order(broker):
    foreign = Order("example", Asset("BBB"), 1, "buy", identifier=100)
    foreign.broker_client_id = 22
    assert not broker._refresh_missing_active_order_from_broker(foreign, "example")
    assert foreign.quantity == 1


def test_tracked_lookup_skips_foreign_order_with_equal_number(broker):
    foreign = Order("example", Asset("BBB"), 1, "buy", identifier=100)
    foreign.broker_client_id = 22
    own = Order("example", Asset("BBB"), 1, "buy", identifier=100)
    own.broker_client_id = 7
    harness = SimpleNamespace(
        _tracked_orders=[foreign, own], _owns_order=broker._owns_order,
        identifiers_equal=broker.identifiers_equal,
    )
    assert Broker.get_tracked_order(harness, 100) is own


def test_subaccount_execution_uses_acct_number(broker):
    broker.subaccount = "synthetic-account"
    original = Order("example", Asset("BBB"), 11, "buy", identifier=100)
    broker.get_tracked_order = Mock(return_value=original)
    broker._process_trade_event = Mock()
    execution = SimpleNamespace(
        clientId=7, acctNumber=broker.subaccount, orderId=100,
        permId=2002, cumQty=5, price=100, shares=5,
    )
    assert broker.on_trade_event(0, None, execution)
    assert original.broker_perm_id == 2002
    assert broker._process_trade_event.call_args.args[1] == broker.PARTIALLY_FILLED_ORDER


def test_incomplete_snapshot_is_not_an_empty_success(broker):
    broker.ib.get_open_orders = lambda: None
    with pytest.raises(TimeoutError, match="snapshot"):
        broker._pull_broker_all_orders()


def test_restart_recovers_own_active_orders_only(broker):
    broker._first_iteration = True
    broker.get_all_orders = lambda: []
    broker._process_broker_synced_order = Mock()
    broker._invalidate_order_caches = Mock()
    broker.sync_orders("example")
    recovered = broker._process_broker_synced_order.call_args.args[0]
    assert recovered.broker_client_id == 7
    broker._process_broker_synced_order.assert_called_once()


def test_informational_warning_does_not_reject_submission():
    app = app_with_ids()
    app._pending_submissions[100] = {"ready": Event(), "error": None}
    app.error(100, 2109, "synthetic warning")
    assert not app._pending_submissions[100]["ready"].is_set()


def test_non_order_request_error_does_not_reject_matching_pending_order_id():
    app = app_with_ids()
    app._pending_submissions[100] = {"ready": Event(), "error": None}
    app.error(100, 200, "synthetic contract-details request error")
    assert not app._pending_submissions[100]["ready"].is_set()
    assert app._pending_submissions[100]["error"] is None


def test_non_order_error_leaves_submission_outcome_unknown_without_retry():
    app = app_with_ids()
    app.client_id = 7
    app.subaccount = None
    app.wrapper = app
    app.ib_broker = None
    app.create_contract = Mock(return_value=Contract())
    app.placeOrder = Mock(side_effect=lambda identifier, *_: app.error(
        identifier, 200, "synthetic contract-details request error",
    ))
    order = Order("example", Asset("BBB"), 1, "buy", quote=Asset("USD", asset_type="forex"))
    with pytest.raises(TimeoutError, match="unknown; not retrying"):
        app.execute_order(order, timeout=0.01)
    app.placeOrder.assert_called_once()


@pytest.mark.parametrize("error_code", [103, 201, 202])
def test_documented_order_error_codes_reject_pending_submissions(error_code):
    app = app_with_ids()
    pending = {"ready": Event(), "error": None}
    app._pending_submissions[100] = pending
    app.error(100, error_code, "synthetic order rejection")
    assert pending["ready"].is_set()
    assert pending["error"][0] == error_code


def test_pending_submit_open_order_does_not_hide_following_duplicate_rejection():
    app = app_with_ids()
    app.client_id = 7
    pending = {"ready": Event(), "error": None}
    app._pending_submissions[100] = pending
    native = NativeOrder()
    native.clientId = 7
    app.openOrder(100, Contract(), native, SimpleNamespace(status="PendingSubmit"))
    assert not pending["ready"].is_set()
    app.error(100, 103, "synthetic duplicate order ID")
    assert pending["ready"].is_set()
    assert pending["error"][0] == 103


def test_pending_submit_stages_bracket_until_transmitting_child_is_accepted():
    app = app_with_ids()
    app.client_id = 7
    app.subaccount = None
    app.wrapper = app
    app.ib_broker = SimpleNamespace(
        _unprocessed_orders=SafeList(None), _invalidate_order_caches=Mock(),
        _process_trade_event=Mock(), ERROR_ORDER="error",
    )
    app.create_contract = Mock(return_value=Contract())
    order = Order(
        "example",
        Asset("BBB"),
        1,
        "buy",
        order_class="bracket",
        secondary_limit_price=110,
        secondary_stop_price=90,
        quote=Asset("USD", asset_type=Asset.AssetType.FOREX),
    )
    sent = []

    def place(identifier, contract, native):
        sent.append((identifier, contract, native))
        if native.transmit:
            for prior_id, prior_contract, prior_native in sent:
                app.openOrder(
                    prior_id, prior_contract, prior_native, SimpleNamespace(status="Submitted"),
                )
        else:
            app.openOrder(identifier, contract, native, SimpleNamespace(status="PendingSubmit"))

    app.placeOrder = Mock(side_effect=place)

    app.execute_order(order, timeout=0.01)

    assert len(sent) == 3
    assert [native.transmit for _, _, native in sent] == [False, False, True]
    assert app._pending_submissions == {}


def test_rejection_after_pending_submit_prevents_sending_child_orders():
    app = app_with_ids()
    app.client_id = 7
    app.subaccount = None
    app.wrapper = app
    app.ib_broker = SimpleNamespace(
        _unprocessed_orders=SafeList(None), _invalidate_order_caches=Mock(),
        _process_trade_event=Mock(), ERROR_ORDER="error",
    )
    app.create_contract = Mock(return_value=Contract())
    order = Order(
        "example",
        Asset("BBB"),
        1,
        "buy",
        order_class="bracket",
        secondary_stop_price=90,
        quote=Asset("USD", asset_type=Asset.AssetType.FOREX),
    )

    def place(identifier, contract, native):
        app.openOrder(identifier, contract, native, SimpleNamespace(status="PendingSubmit"))
        app.error(identifier, 201, "synthetic staged-order rejection")

    app.placeOrder = Mock(side_effect=place)

    with pytest.raises(RuntimeError, match="rejected"):
        app.execute_order(order, timeout=0.01)

    app.placeOrder.assert_called_once()
    assert app._pending_submissions == {}


def test_rejection_after_staged_leg_final_status_is_not_lost():
    app = app_with_ids()
    app.client_id = 7
    app.subaccount = None
    app.wrapper = app
    app.ib_broker = SimpleNamespace(
        _unprocessed_orders=SafeList(None), _invalidate_order_caches=Mock(),
        _process_trade_event=Mock(), ERROR_ORDER="error",
    )
    app.create_contract = Mock(return_value=Contract())
    order = Order(
        "example",
        Asset("BBB"),
        1,
        "buy",
        order_class="bracket",
        secondary_stop_price=90,
        quote=Asset("USD", asset_type=Asset.AssetType.FOREX),
    )
    sent = []

    def place(identifier, contract, native):
        sent.append((identifier, contract, native))
        if native.transmit:
            for prior_id, prior_contract, prior_native in sent:
                app.openOrder(
                    prior_id, prior_contract, prior_native, SimpleNamespace(status="Submitted"),
                )
            app.error(sent[0][0], 201, "synthetic late staged-order rejection")
        else:
            app.openOrder(identifier, contract, native, SimpleNamespace(status="PendingSubmit"))

    app.placeOrder = Mock(side_effect=place)

    with pytest.raises(RuntimeError, match="rejected"):
        app.execute_order(order, timeout=0.01)

    assert len(sent) == 2
    assert app._pending_submissions == {}


def test_duplicate_id_after_staging_rekeys_bracket_before_transmission():
    app = app_with_ids()
    app.client_id = 7
    app.subaccount = None
    app.wrapper = app
    app.ib_broker = SimpleNamespace(
        _unprocessed_orders=SafeList(None), _invalidate_order_caches=Mock(),
        _process_trade_event=Mock(), ERROR_ORDER="error",
    )
    app.create_contract = Mock(return_value=Contract())
    order = Order(
        "example",
        Asset("BBB"),
        1,
        "buy",
        order_class="bracket",
        secondary_limit_price=110,
        secondary_stop_price=90,
        quote=Asset("USD", asset_type=Asset.AssetType.FOREX),
    )
    sent = []

    def place(identifier, contract, native):
        sent.append((identifier, contract, native))
        if len(sent) == 1:
            app.openOrder(identifier, contract, native, SimpleNamespace(status="PendingSubmit"))
            app.error(identifier, 103, "synthetic duplicate after staging")
        elif native.transmit:
            for prior_id, prior_contract, prior_native in sent[1:]:
                app.openOrder(
                    prior_id, prior_contract, prior_native, SimpleNamespace(status="Submitted"),
                )
        else:
            app.openOrder(identifier, contract, native, SimpleNamespace(status="PendingSubmit"))

    app.placeOrder = Mock(side_effect=place)

    app.execute_order(order, timeout=0.01)

    assert len(sent) == 4
    assert len({identifier for identifier, _, _ in sent}) == 4
    assert [native.transmit for _, _, native in sent] == [False, False, False, True]
    assert all(child.parent_identifier == order.identifier for child in order.child_orders)
    assert all(native.parentId == order.identifier for _, _, native in sent[1:] if native.parentId)
    assert app._pending_submissions == {}


def test_late_inactive_callback_for_duplicate_cannot_cancel_replacement(broker):
    app = app_with_ids()
    broker.ib = app
    app._pending_submissions[100] = {"ready": Event(), "error": None}
    app.error(100, 103, "synthetic duplicate")
    app._pending_submissions.clear()
    broker.get_tracked_order = Mock()
    broker.on_status_event(100, "Inactive", 0, 1, 0, 0, 0, 0, 7, "", 0)
    broker.get_tracked_order.assert_not_called()


def test_direct_lookup_rejects_contract_mismatch(broker):
    original = Order("example", Asset("AAA"), 35, "buy", identifier=100)
    original.status = Order.OrderStatus.SUBMITTED
    assert not broker._refresh_missing_active_order_from_broker(original, "example")
    assert original.quantity == 35


def test_executor_releases_held_events_after_snapshot_failure(broker):
    executor = object.__new__(StrategyExecutor)
    executor.broker = broker
    broker._hold_trade_events = True
    broker.process_held_trades = Mock()
    executor._sync_broker = Mock(side_effect=TimeoutError("synthetic incomplete snapshot"))
    with pytest.raises(TimeoutError):
        executor.sync_broker()
    assert broker._hold_trade_events is False
    broker.process_held_trades.assert_called_once()


def test_submission_timeout_is_not_retried():
    app = app_with_ids()
    app.client_id = 7
    app.subaccount = None
    app.wrapper = app
    app.ib_broker = None
    app.create_contract = Mock(return_value=Contract())
    app.placeOrder = Mock()
    order = Order("example", Asset("BBB"), 1, "buy", quote=Asset("USD", asset_type="forex"))
    with pytest.raises(TimeoutError, match="unknown; not retrying"):
        app.execute_order(order, timeout=0.01)
    app.placeOrder.assert_called_once()
    assert app._pending_submissions == {}


def test_duplicate_with_missing_retry_id_is_reported_as_error():
    app = app_with_ids()
    app.client_id = 7
    app.subaccount = None
    app.wrapper = app
    app.ib_broker = SimpleNamespace(
        _unprocessed_orders=SafeList(None), _process_trade_event=Mock(), ERROR_ORDER="error",
    )
    app.reqIds = Mock()

    def request(_):
        if app.reqIds.call_count == 1:
            app.nextValidId(100)

    app.reqIds.side_effect = request
    app.create_contract = Mock(return_value=Contract())
    app.placeOrder = Mock(side_effect=lambda identifier, *_: app.error(identifier, 103, "synthetic duplicate"))
    order = Order("example", Asset("BBB"), 1, "buy", quote=Asset("USD", asset_type="forex"))
    with pytest.raises(TimeoutError, match="nextValidId"):
        app.execute_order(order, timeout=0.01)
    app.placeOrder.assert_called_once()
    assert app.ib_broker._process_trade_event.call_args.args == (order, "error")


@pytest.mark.parametrize("order_class,rejected_index", [
    ("bracket", 0), ("bracket", 1), ("bracket", 2), ("oco", 0), ("oco", 1),
])
def test_advanced_duplicate_retry_updates_tracked_ids_and_parent_links(order_class, rejected_index):
    app = app_with_ids()
    app.client_id = 7
    app.subaccount = "synthetic-account"
    app.wrapper = app
    app.ib_broker = SimpleNamespace(
        _unprocessed_orders=SafeList(None), _invalidate_order_caches=Mock(),
    )
    app.create_contract = Mock(return_value=Contract())
    prices = (
        {"secondary_limit_price": 110, "secondary_stop_price": 90}
        if order_class == "bracket" else {"limit_price": 110, "stop_price": 90}
    )
    order = Order(
        "example", Asset("BBB"), 1, "buy", order_class=order_class,
        quote=Asset("USD", asset_type="forex"), **prices,
    )
    sent = []
    rejected = False

    def place(identifier, contract, native):
        nonlocal rejected
        assert native.account == app.subaccount
        assert any(item.identifier == identifier for item in app.ib_broker._unprocessed_orders)
        sent.append((identifier, native.parentId, native.transmit, native.ocaGroup))
        if len(sent) - 1 == rejected_index and not rejected:
            rejected = True
            app.error(identifier, 103, "synthetic duplicate")
        else:
            app._submission_response(identifier)

    app.placeOrder = Mock(side_effect=place)
    app.execute_order(order)
    leg_count = 3 if order_class == "bracket" else 2
    assert len(sent) == leg_count + 1
    assert len({entry[0] for entry in sent}) == leg_count + 1
    successful = [entry for index, entry in enumerate(sent) if index != rejected_index]
    if order_class == "bracket":
        assert all(child.parent_identifier == order.identifier for child in order.child_orders)
        assert [entry[0] for entry in successful] == [
            order.identifier, *[child.identifier for child in order.child_orders],
        ]
        assert [entry[1] for entry in successful] == [0, order.identifier, order.identifier]
    else:
        assert [entry[0] for entry in successful] == [child.identifier for child in order.child_orders]
        assert [entry[1] for entry in successful] == [0, 0]
        assert len({entry[3] for entry in successful}) == 1
