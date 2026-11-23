"""Regression tests for malkam03/lumibot#17.

Auto-built OTO/BRACKET/OCO child orders must inherit the parent's
``time_in_force`` and ``good_till_date``. Before the fix they kept the ``"day"``
default, so a GTC advanced order got DAY children, and IBKR's TWS child
validation (which requires children to match the parent) rejected the order
before it was sent.
"""

import datetime
from types import SimpleNamespace

import pytest

from lumibot.backtesting.backtesting_broker import BacktestingBroker
from lumibot.brokers.interactive_brokers import IBApp
from lumibot.entities import Asset, Order

GTD = datetime.datetime(2030, 1, 15, 16, 0, tzinfo=datetime.timezone.utc)


def _oco(time_in_force="gtc", good_till_date=None):
    return Order(
        strategy="example",
        asset=Asset("TEST"),
        quantity=100,
        side="sell",
        limit_price=11.0,
        stop_price=9.0,
        time_in_force=time_in_force,
        good_till_date=good_till_date,
        order_class=Order.OrderClass.OCO,
    )


def _order(order_class, time_in_force="gtc", good_till_date=None, **kwargs):
    return Order(
        strategy="example",
        asset=Asset("TEST"),
        quantity=100,
        side="buy",
        order_type=Order.OrderType.LIMIT,
        limit_price=10.0,
        time_in_force=time_in_force,
        good_till_date=good_till_date,
        order_class=order_class,
        **kwargs,
    )


def _assert_children_match_parent(order, expected_count):
    assert len(order.child_orders) == expected_count
    for child in order.child_orders:
        assert child.time_in_force == order.time_in_force
        assert child.good_till_date == order.good_till_date


class TestOrderChildTimeInForce:
    @pytest.mark.parametrize(
        "secondary",
        [{"secondary_stop_price": 9.0}, {"secondary_limit_price": 11.0}],
        ids=["stop_child", "limit_child"],
    )
    @pytest.mark.parametrize("tif", ["gtc", "day", "ioc"])
    def test_oto_child_inherits_parent_time_in_force(self, tif, secondary):
        order = _order(Order.OrderClass.OTO, time_in_force=tif, **secondary)
        _assert_children_match_parent(order, 1)
        assert order.child_orders[0].time_in_force == tif

    @pytest.mark.parametrize("tif", ["gtc", "day"])
    def test_bracket_children_inherit_parent_time_in_force(self, tif):
        order = _order(
            Order.OrderClass.BRACKET,
            time_in_force=tif,
            secondary_stop_price=9.0,
            secondary_limit_price=11.0,
        )
        _assert_children_match_parent(order, 2)
        assert {c.time_in_force for c in order.child_orders} == {tif}

    def test_bracket_single_stop_child_inherits_time_in_force(self):
        order = _order(Order.OrderClass.BRACKET, secondary_stop_price=9.0)
        _assert_children_match_parent(order, 1)

    @pytest.mark.parametrize(
        "order_class, secondary",
        [
            (Order.OrderClass.OTO, {"secondary_stop_price": 9.0}),
            (Order.OrderClass.BRACKET, {"secondary_stop_price": 9.0, "secondary_limit_price": 11.0}),
        ],
        ids=["oto", "bracket"],
    )
    def test_children_inherit_good_till_date(self, order_class, secondary):
        order = _order(order_class, time_in_force="gtd", good_till_date=GTD, **secondary)
        _assert_children_match_parent(order, len(secondary))
        assert all(c.good_till_date == GTD for c in order.child_orders)

    @pytest.mark.parametrize("tif", ["gtc", "day"])
    def test_oco_children_inherit_parent_time_in_force(self, tif):
        order = _oco(time_in_force=tif)
        _assert_children_match_parent(order, 2)
        assert {c.time_in_force for c in order.child_orders} == {tif}

    def test_oco_children_inherit_good_till_date(self):
        order = _oco(time_in_force="gtd", good_till_date=GTD)
        _assert_children_match_parent(order, 2)
        assert all(c.good_till_date == GTD for c in order.child_orders)

    def test_stop_modifiers_still_applied_to_inherited_children(self):
        order = _order(
            Order.OrderClass.BRACKET,
            secondary_stop_price=9.0,
            secondary_stop_limit_price=8.9,
            secondary_limit_price=11.0,
        )
        limit_child, stop_child = order.child_orders
        assert limit_child.order_type == Order.OrderType.LIMIT
        assert stop_child.order_type == Order.OrderType.STOP_LIMIT
        assert stop_child.time_in_force == "gtc"

    def test_default_parent_time_in_force_keeps_children_on_day(self):
        order = Order(
            strategy="example",
            asset=Asset("TEST"),
            quantity=100,
            side="buy",
            order_type=Order.OrderType.LIMIT,
            limit_price=10.0,
            order_class=Order.OrderClass.OTO,
            secondary_stop_price=9.0,
        )
        assert order.time_in_force == "day"
        _assert_children_match_parent(order, 1)

    def test_user_supplied_children_are_not_overwritten(self):
        child = Order(
            strategy="example",
            asset=Asset("TEST"),
            quantity=100,
            side="sell",
            stop_price=9.0,
            time_in_force="day",
        )
        order = _order(Order.OrderClass.OTO, child_orders=[child])
        assert order.child_orders == [child]
        assert order.child_orders[0].time_in_force == "day"


class TestBacktestingFlattenLegacyChildTimeInForce:
    """The legacy ``_flatten_order`` fallback builds BRACKET/OTO children too."""

    @staticmethod
    def _childless(order_class, secondary_stop_price=None, secondary_limit_price=None):
        order = _order(
            order_class,
            time_in_force="gtd",
            good_till_date=GTD,
            secondary_stop_price=secondary_stop_price,
            secondary_limit_price=secondary_limit_price,
        )
        # Force the legacy path: no pre-built children, secondary prices on the order.
        order.child_orders = []
        order.secondary_stop_price = secondary_stop_price
        order.secondary_limit_price = secondary_limit_price
        return order

    def test_bracket_fallback_children_inherit_time_in_force(self):
        order = self._childless(Order.OrderClass.BRACKET, 9.0, 11.0)
        flattened = BacktestingBroker._flatten_order(SimpleNamespace(), order)
        assert flattened[0] is order
        children = flattened[1:]
        assert len(children) == 2
        for child in children:
            assert child.time_in_force == "gtd"
            assert child.good_till_date == GTD

    def test_oco_fallback_children_inherit_time_in_force(self):
        order = _oco(time_in_force="gtd", good_till_date=GTD)
        order.child_orders = []
        flattened = BacktestingBroker._flatten_order(SimpleNamespace(), order)
        # OCO excludes the parent; only the two exits are returned.
        assert len(flattened) == 2
        for child in flattened:
            assert child.time_in_force == "gtd"
            assert child.good_till_date == GTD

    def test_oto_fallback_child_inherits_time_in_force(self):
        order = self._childless(Order.OrderClass.OTO, secondary_stop_price=9.0)
        flattened = BacktestingBroker._flatten_order(SimpleNamespace(), order)
        assert len(flattened) == 2
        assert flattened[1].time_in_force == "gtd"
        assert flattened[1].good_till_date == GTD


class TestTwsAdvancedChildValidation:
    """The issue's reproduction against IBApp._validate_advanced_children."""

    BROKER = SimpleNamespace(get_safe_action=lambda side: IBApp.get_safe_action(None, side))

    @pytest.mark.parametrize("tif, gtd", [("gtc", None), ("day", None), ("gtd", GTD)])
    @pytest.mark.parametrize(
        "build",
        [
            lambda tif, gtd: _order(Order.OrderClass.OTO, tif, gtd, secondary_stop_price=9.0),
            lambda tif, gtd: _order(Order.OrderClass.OTO, tif, gtd, secondary_limit_price=11.0),
            lambda tif, gtd: _order(
                Order.OrderClass.BRACKET, tif, gtd, secondary_stop_price=9.0, secondary_limit_price=11.0
            ),
            lambda tif, gtd: _order(Order.OrderClass.BRACKET, tif, gtd, secondary_stop_price=9.0),
            lambda tif, gtd: _oco(tif, gtd),
        ],
        ids=["oto_stop", "oto_limit", "bracket", "bracket_stop_only", "oco"],
    )
    def test_auto_built_children_pass_tws_validation(self, build, tif, gtd):
        IBApp._validate_advanced_children(self.BROKER, build(tif, gtd))

    def test_validator_still_rejects_mismatched_child(self):
        order = _order(Order.OrderClass.OTO, secondary_stop_price=9.0)
        order.child_orders[0].time_in_force = "day"
        with pytest.raises(ValueError, match="time_in_force and good_till_date must match parent"):
            IBApp._validate_advanced_children(self.BROKER, order)
