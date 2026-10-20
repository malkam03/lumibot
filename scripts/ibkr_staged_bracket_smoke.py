"""Zero-network smoke check for IBKR staged BRACKET/OTO submission.

Runs against the *installed* ``lumibot`` package (for example a deployed
wheel); it does not need the LumiBot test tree and never opens a socket.
A fake gateway replaces ``placeOrder``/``reqIds`` and behaves like live
IB Gateway: ``transmit=False`` legs get no callback until the graph's
transmitting leg arrives, then every leg is acknowledged.

Usage::

    python scripts/ibkr_staged_bracket_smoke.py

Exit code 0 means every scenario passed; 1 means at least one failed.
"""

import sys
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

from ibapi.contract import Contract
from ibapi.order import Order as NativeOrder

import lumibot
from lumibot.brokers.interactive_brokers import (
    IBApp,
    IBKROrderSubmissionError,
    IBKROrderSubmissionUnknown,
    IBWrapper,
)
from lumibot.entities import Asset, Order
from lumibot.trading_builtins import SafeList

CLIENT_ID = 7


def build_app(acknowledge_graph=True):
    """Return an unconnected IBApp wired to a silent-staged-leg fake gateway."""
    app = object.__new__(IBApp)
    IBWrapper.__init__(app)
    app.wrapper = app
    app.client_id = CLIENT_ID
    app.subaccount = None
    next_id = iter(range(9000, 10000))
    app.reqIds = lambda _num: app.nextValidId(next(next_id))
    app.create_contract = Mock(return_value=Contract())
    app.ib_broker = SimpleNamespace(
        _unprocessed_orders=SafeList(None),
        _invalidate_order_caches=Mock(),
        _process_trade_event=Mock(),
        ERROR_ORDER="error",
    )
    sent = []

    def place(identifier, contract, native):
        sent.append((identifier, native.parentId, native.transmit))
        if native.transmit and acknowledge_graph:
            for leg_id, _, _ in sent:
                ack = NativeOrder()
                ack.clientId = CLIENT_ID
                app.openOrder(leg_id, Contract(), ack, SimpleNamespace(status="PreSubmitted"))

    app.placeOrder = place
    return app, sent


def build_order(order_class):
    prices = {"secondary_stop_price": 90}
    if order_class == Order.OrderClass.BRACKET:
        prices["secondary_limit_price"] = 110
    return Order(
        "smoke", Asset("SMOKE"), 1, "buy", order_class=order_class,
        quote=Asset("USD", asset_type=Asset.AssetType.FOREX), **prices,
    )


def check_graph_acknowledged(order_class):
    app, sent = build_app()
    order = build_order(order_class)
    app.execute_order(order, timeout=2)

    legs = 1 + len(order.child_orders)
    expected_ids = [order.identifier, *[child.identifier for child in order.child_orders]]
    assert [leg[0] for leg in sent] == expected_ids, f"leg ids {sent} != {expected_ids}"
    assert [leg[2] for leg in sent] == [False] * (legs - 1) + [True], f"transmit flags {sent}"
    assert [leg[1] for leg in sent] == [0] + [order.identifier] * (legs - 1), f"parent links {sent}"
    assert len(set(expected_ids)) == legs, "order ids must be unique"
    assert app._pending_submissions == {}, "pending submissions were not cleared"
    app.ib_broker._process_trade_event.assert_not_called()
    for tracked in [order, *order.child_orders]:
        assert tracked.broker_client_id == CLIENT_ID, "client identity not stamped"
    return f"{legs} legs ids={expected_ids} parent={order.identifier}"


def check_missing_final_ack_is_typed_unknown():
    app, sent = build_app(acknowledge_graph=False)
    order = build_order(Order.OrderClass.BRACKET)
    try:
        app.execute_order(order, timeout=0.2)
    except IBKROrderSubmissionUnknown as exc:
        assert isinstance(exc, IBKROrderSubmissionError) and isinstance(exc, TimeoutError)
        assert exc.outcome == IBKROrderSubmissionError.UNKNOWN and not exc.positively_rejected
        assert exc.client_id == CLIENT_ID
        assert list(exc.order_ids) == [leg[0] for leg in sent], "exception ids != sent ids"
        assert len(sent) == 3, "graph must be sent once, never retried"
        return f"outcome={exc.outcome} legs={[leg.outcome for leg in exc.legs]}"
    raise AssertionError("expected IBKROrderSubmissionUnknown")


SCENARIOS = [
    ("bracket graph acknowledged after transmitting leg",
     lambda: check_graph_acknowledged(Order.OrderClass.BRACKET)),
    ("oto graph acknowledged after transmitting leg",
     lambda: check_graph_acknowledged(Order.OrderClass.OTO)),
    ("missing final acknowledgement is typed UNKNOWN, not retried",
     check_missing_final_ack_is_typed_unknown),
]


def main():
    print(f"lumibot {getattr(lumibot, '__version__', 'unknown')} from {lumibot.__file__}")
    failures = 0
    for name, scenario in SCENARIOS:
        try:
            print(f"PASS {name}: {scenario()}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
