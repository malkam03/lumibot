"""Offline contract tests for the legacy TWS native-order converter."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lumibot.brokers.interactive_brokers import IBApp, InteractiveBrokers
from lumibot.entities import Asset, Order
from lumibot.trading_builtins import SafeList


@pytest.fixture
def app():
    client = object.__new__(IBApp)
    client.nextValidOrderId = 2001
    return client


@pytest.fixture
def asset():
    return Asset("TEST", asset_type=Asset.AssetType.STOCK)


def make_order(asset, side="buy", order_class="bracket", **kwargs):
    return Order(
        strategy="conversion-test",
        asset=asset,
        quantity=2,
        side=side,
        order_class=order_class,
        identifier=2000,
        **kwargs,
    )


def assert_common(legs, expected_ids, tif="DAY", good_till_date=""):
    assert [leg.orderId for leg in legs] == expected_ids
    assert [leg.totalQuantity for leg in legs] == [2] * len(legs)
    assert [leg.tif for leg in legs] == [tif] * len(legs)
    assert [leg.goodTillDate for leg in legs] == [good_till_date] * len(legs)
    assert all(leg.outsideRth is False for leg in legs)
    assert all(leg.eTradeOnly is False and leg.firmQuoteOnly is False for leg in legs)


@pytest.mark.parametrize("side,entry_action,exit_action", [
    ("buy", "BUY", "SELL"),
    ("sell_short", "SELL", "BUY"),
])
@pytest.mark.parametrize("entry_price,entry_type", [(None, "MKT"), (101, "LMT")])
def test_bracket_uses_generated_exits_and_stages_parent(
    app, asset, side, entry_action, exit_action, entry_price, entry_type,
):
    order = make_order(
        asset, side=side, limit_price=entry_price, secondary_limit_price=117,
        secondary_stop_price=83,
    )
    assert len(order.child_orders) == 2

    parent, target, stop = app.create_order(order)

    assert_common([parent, target, stop], [2000, 2001, 2002])
    assert [leg.action for leg in (parent, target, stop)] == [entry_action, exit_action, exit_action]
    assert [leg.orderType for leg in (parent, target, stop)] == [entry_type, "LMT", "STP"]
    if entry_price is not None:
        assert parent.lmtPrice == entry_price
    assert target.lmtPrice == 117
    assert stop.auxPrice == 83
    assert [target.parentId, stop.parentId] == [parent.orderId] * 2
    assert [leg.transmit for leg in (parent, target, stop)] == [False, False, True]


@pytest.mark.parametrize("side,entry_action,exit_action", [
    ("buy", "BUY", "SELL"),
    ("sell_short", "SELL", "BUY"),
])
@pytest.mark.parametrize("entry_price,entry_type", [(None, "MKT"), (101, "LMT")])
def test_oto_stop_preserves_child_type_and_price(app, asset, side, entry_action, exit_action, entry_price, entry_type):
    order = make_order(asset, side=side, order_class="oto", limit_price=entry_price, secondary_stop_price=83)
    assert len(order.child_orders) == 1

    parent, stop = app.create_order(order)

    assert_common([parent, stop], [2000, 2001])
    assert [parent.action, stop.action] == [entry_action, exit_action]
    assert [parent.orderType, stop.orderType] == [entry_type, "STP"]
    if entry_price is not None:
        assert parent.lmtPrice == entry_price
    assert stop.auxPrice == 83
    assert stop.parentId == parent.orderId
    assert [parent.transmit, stop.transmit] == [False, True]


def test_oto_limit_exit(app, asset):
    parent, target = app.create_order(
        make_order(asset, order_class="oto", limit_price=101, secondary_limit_price=117)
    )
    assert_common([parent, target], [2000, 2001])
    assert [parent.lmtPrice, target.lmtPrice] == [101, 117]
    assert [parent.orderType, target.orderType] == ["LMT", "LMT"]
    assert target.parentId == parent.orderId
    assert [parent.transmit, target.transmit] == [False, True]


@pytest.mark.parametrize("side,action", [("sell_to_close", "SELL"), ("buy_to_cover", "BUY")])
def test_oco_peers_transmit_independently(app, asset, side, action):
    order = make_order(asset, side=side, order_class="oco", limit_price=117, stop_price=83)
    assert len(order.child_orders) == 2

    target, stop = app.create_order(order)

    assert_common([target, stop], [2000, 2001])
    assert [target.action, stop.action] == [action, action]
    assert [target.orderType, stop.orderType] == ["LMT", "STP"]
    assert target.lmtPrice == 117
    assert stop.auxPrice == 83
    assert target.ocaGroup and target.ocaGroup == stop.ocaGroup
    assert [target.ocaType, stop.ocaType] == [1, 1]
    assert [target.parentId, stop.parentId] == [0, 0]
    assert [target.transmit, stop.transmit] == [True, True]


@pytest.mark.parametrize("side,action", [
    ("buy", "BUY"), ("buy_to_open", "BUY"), ("buy_to_close", "BUY"),
    ("buy_to_cover", "BUY"), ("sell", "SELL"), ("sell_short", "SELL"),
    ("sell_to_open", "SELL"), ("sell_to_close", "SELL"),
])
def test_retail_actions(app, side, action):
    assert app.get_safe_action(side) == action


def test_matching_duration_on_explicit_children(app, asset):
    expiry = datetime(2026, 11, 2, 16)
    child = Order(
        strategy="conversion-test", asset=asset, quantity=2, side="sell",
        stop_price=83, time_in_force="gtd", good_till_date=expiry,
    )
    order = make_order(
        asset, order_class="oto", secondary_stop_price=83,
        child_orders=[child], time_in_force="gtd", good_till_date=expiry,
    )
    assert_common(app.create_order(order), [2000, 2001], "GTD", "20261102 16:00:00")


@pytest.mark.parametrize("order_class,prices", [
    ("bracket", {"secondary_limit_price": 117, "secondary_stop_price": 83}),
    ("oto", {"secondary_stop_price": 83}),
    ("oco", {"limit_price": 117, "stop_price": 83}),
])
def test_generated_day_child_of_gtc_parent_is_rejected(app, asset, order_class, prices):
    order = make_order(asset, order_class=order_class, time_in_force="gtc", **prices)
    assert all(child.time_in_force == "day" for child in order.child_orders)
    with pytest.raises(ValueError, match="time_in_force"):
        app.create_order(order)
    assert app.nextValidOrderId == 2001


def test_matching_gtc_on_explicit_children(app, asset):
    child = Order(
        strategy="conversion-test", asset=asset, quantity=2, side="sell",
        limit_price=117, time_in_force="gtc",
    )
    order = make_order(asset, order_class="oto", child_orders=[child], time_in_force="gtc")
    assert_common(app.create_order(order), [2000, 2001], "GTC")


def test_single_child_bracket_and_integer_ids_for_unassigned_parent(app, asset):
    order = make_order(asset, secondary_stop_price=83)
    order.identifier = "unassigned"
    parent, stop = app.create_order(order)
    assert [parent.orderId, stop.orderId] == [2001, 2002]
    assert stop.parentId == parent.orderId
    assert [parent.transmit, stop.transmit] == [False, True]


def test_oco_groups_are_unique_across_orders(app, asset):
    first = make_order(asset, side="sell", order_class="oco", limit_price=117, stop_price=83)
    second = make_order(asset, side="sell", order_class="oco", limit_price=117, stop_price=83)
    second.identifier = 2002
    first_group = app.create_order(first)[0].ocaGroup
    second_group = app.create_order(second)[0].ocaGroup
    assert first_group != second_group


@pytest.mark.parametrize("change,match", [
    (lambda child: setattr(child, "quantity", 1), "quantity"),
    (lambda child: setattr(child, "time_in_force", "gtc"), "time_in_force"),
    (lambda child: setattr(child, "good_till_date", datetime(2026, 11, 2)), "good_till_date"),
    (lambda child: setattr(child, "side", "buy"), "side"),
    (lambda child: setattr(child, "order_type", Order.OrderType.TRAIL), "child"),
    (lambda child: setattr(child, "order_type", Order.OrderType.STOP_LIMIT), "child"),
    (lambda child: child.child_orders.append(child), "child"),
])
def test_invalid_child_graph_fails_before_allocating_ids(app, asset, change, match):
    order = make_order(asset, order_class="oto", secondary_stop_price=83)
    change(order.child_orders[0])
    with pytest.raises(ValueError, match=match):
        app.create_order(order)
    assert app.nextValidOrderId == 2001


@pytest.mark.parametrize("order_class,child_count", [
    ("bracket", 0), ("bracket", 3), ("oto", 0), ("oto", 2), ("oco", 0), ("oco", 1), ("oco", 3),
])
def test_invalid_child_count_rejected(app, asset, order_class, child_count):
    order = make_order(
        asset, order_class=order_class, limit_price=101,
        stop_price=83 if order_class == "oco" else None,
        secondary_limit_price=117 if order_class != "oto" else None,
        secondary_stop_price=83,
    )
    order.child_orders = [order.child_orders[0]] * child_count
    with pytest.raises(ValueError, match="child"):
        app.create_order(order)
    assert app.nextValidOrderId == 2001


def test_duplicate_child_is_not_a_valid_bracket_graph(app, asset):
    order = make_order(asset, secondary_limit_price=117, secondary_stop_price=83)
    order.child_orders[1] = order.child_orders[0]
    with pytest.raises(ValueError, match="distinct"):
        app.create_order(order)
    assert app.nextValidOrderId == 2001


def test_two_bracket_targets_without_stop_are_rejected(app, asset):
    order = make_order(asset, secondary_limit_price=117, secondary_stop_price=83)
    order.child_orders[1] = Order(
        strategy="conversion-test", asset=asset, quantity=2, side="sell", limit_price=121,
    )
    with pytest.raises(ValueError, match="limit and a stop"):
        app.create_order(order)
    assert app.nextValidOrderId == 2001


def test_simple_order_retains_type_fields_and_duration(app, asset):
    order = make_order(asset, order_class="simple", stop_price=83, time_in_force="gtc")
    (native,) = app.create_order(order)
    assert_common([native], [2000], "GTC")
    assert native.orderType == "STP"
    assert native.auxPrice == 83


def test_stop_limit_uses_stop_limit_price_and_rejects_missing_limit(app, asset):
    order = make_order(
        asset, order_class="simple", order_type="stop_limit",
        stop_price=83, stop_limit_price=82,
    )
    (native,) = app.create_order(order)
    assert native.orderType == "STP LMT"
    assert native.auxPrice == 83
    assert native.lmtPrice == 82

    order.stop_limit_price = None
    with pytest.raises(ValueError, match="stop_limit_price"):
        app.create_order(order)


def test_aware_gtd_is_sent_in_utc(app, asset):
    local_expiry = datetime(2026, 11, 2, 16, tzinfo=timezone(timedelta(hours=-5)))
    order = make_order(
        asset, order_class="simple", limit_price=101, time_in_force="gtd",
        good_till_date=local_expiry,
    )
    (native,) = app.create_order(order)
    assert native.goodTillDate == "20261102 21:00:00 UTC"
    child = Order(
        strategy="conversion-test", asset=asset, quantity=2, side="sell",
        stop_price=83, time_in_force="gtd", good_till_date=local_expiry,
    )
    attached = make_order(
        asset, order_class="oto", child_orders=[child],
        time_in_force="gtd", good_till_date=local_expiry,
    )
    assert [
        leg.goodTillDate for leg in app.create_order(attached)
    ] == ["20261102 21:00:00 UTC"] * 2


def test_stop_limit_parent_retains_both_prices(app, asset):
    order = make_order(
        asset, order_type="stop_limit", stop_price=101, stop_limit_price=102,
        secondary_stop_price=83,
    )
    parent, stop = app.create_order(order)
    assert parent.orderType == "STP LMT"
    assert parent.auxPrice == 101
    assert parent.lmtPrice == 102
    assert stop.orderType == "STP"
    assert stop.auxPrice == 83


@pytest.mark.parametrize("order_class,prices,expected_ids", [
    ("simple", {"limit_price": 101}, [2001]),
    ("bracket", {"secondary_limit_price": 117, "secondary_stop_price": 83}, [2001, 2002, 2003]),
    ("oto", {"secondary_stop_price": 83}, [2001, 2002]),
    ("oco", {"limit_price": 117, "stop_price": 83}, [2001, 2002]),
])
def test_submitted_children_are_tracked_before_native_placement(
    app, asset, order_class, prices, expected_ids,
):
    broker = object.__new__(InteractiveBrokers)
    broker.ib = app
    broker.subaccount = None
    broker._unprocessed_orders = SafeList(None)
    broker.get_tracked_order = lambda identifier: next(
        (item for item in broker._unprocessed_orders if item.identifier == identifier), None
    )
    broker.order_status_duplicates = []
    broker._process_trade_event = Mock()
    app.ib_broker = broker
    app.wrapper = SimpleNamespace(init_new_orders=lambda: None)
    app.create_contract = lambda *args, **kwargs: object()
    app.placeOrder = Mock(side_effect=lambda native_id, contract, leg: (
        broker.get_tracked_order(native_id) is not None
        or (_ for _ in ()).throw(AssertionError("native leg placed before tracking"))
    ))
    order = make_order(asset, order_class=order_class, **prices)
    order.quote = Asset("USD", asset_type=Asset.AssetType.FOREX)
    for child in order.child_orders:
        child.quote = order.quote

    broker._submit_order(order)

    assert [call.args[0] for call in app.placeOrder.call_args_list] == expected_ids
    assert [child.identifier for child in order.child_orders] == (
        expected_ids[1:] if order_class != "oco" else expected_ids
    )
    assert [item.identifier for item in broker._unprocessed_orders] == expected_ids
    if order_class != "oco":
        assert all(child.parent_identifier == order.identifier for child in order.child_orders)
    for child in order.child_orders:
        broker.on_status_event(child.identifier, "Submitted", 0, 2, 0, 0, 0, 0, 0, "", 0)
        broker.on_trade_event(
            0, None, SimpleNamespace(orderId=child.identifier, cumQty=2, price=95, shares=2)
        )
    assert broker._process_trade_event.call_count == 2 * len(order.child_orders)
    assert [
        call.args[0] for call in broker._process_trade_event.call_args_list
    ] == [child for child in order.child_orders for _ in range(2)]
