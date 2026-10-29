"""Opt-in, one-share paper OTO probe using the installed LumiBot socket adapter.

No connection is opened without --execute-paper. Failures never submit a
replacement exit or cancel protection: reconcile the tagged IDs manually.
"""

import argparse
import datetime as dt
import logging
import math
import queue
import sys
import time
import uuid
from threading import Event, Thread
from types import SimpleNamespace

from ibapi.contract import Contract
from ibapi.order import Order as NativeOrder

from lumibot.brokers.interactive_brokers import IBApp, IBClient, IBWrapper, InteractiveBrokers
from lumibot.entities import Asset, Order
from lumibot.strategies.strategy import Strategy


def require_paper_account(accounts):
    """Fail closed on mixed, missing, live or multiple managed accounts."""
    accounts = [account.strip() for account in accounts if account.strip()]
    if len(accounts) != 1 or not accounts[0].startswith("DU") or not accounts[0][2:].isdigit():
        raise RuntimeError("Probe requires exactly one DU-prefixed IBKR paper account.")
    return accounts[0]


def require_trading_window(now=None):
    """Respect actual NYSE sessions, including holidays and early closes."""
    import pandas_market_calendars as mcal

    now = now or dt.datetime.now(dt.timezone.utc)
    date = now.astimezone(dt.timezone.utc).date()
    schedule = mcal.get_calendar("NYSE").schedule(start_date=date, end_date=date)
    if schedule.empty:
        raise RuntimeError("No NYSE session today; no probe orders permitted.")
    row = schedule.iloc[0]
    if not row.market_open.to_pydatetime() <= now < row.market_close.to_pydatetime() - dt.timedelta(minutes=15):
        raise RuntimeError("Probe requires an open NYSE session with at least 15 minutes remaining.")


def unused_symbol(symbols, positions, orders):
    """Exclude any symbol exposed or ordered in any visible account/client."""
    if positions is None or orders is None:
        raise RuntimeError("Complete broker positions and orders are required.")
    blocked = {position["symbol"] for position in positions if position["position"] != 0}
    blocked.update(order.contract.symbol for order in orders)
    for symbol in symbols:
        if symbol not in blocked:
            return symbol
    raise RuntimeError("No candidate has zero existing exposure and zero open orders.")


def checked_quote(app, symbol):
    bid, ask = app.quote_prices(symbol)
    if not all(math.isfinite(value) and value > 0 for value in (bid, ask)):
        raise RuntimeError("Probe requires positive finite real-time bid/ask.")
    if ask < bid or ask / bid > 1.01:
        raise RuntimeError("Probe quote is crossed or too wide.")
    return bid, ask


def wait_until(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.2)
    raise TimeoutError(f"Probe timed out waiting for {description}; reconcile tagged orders, no retry.")


def run_probe(app, symbols, timeout=15, max_notional=1000):
    """Run only on a connected, exclusively paper session; returns sanitized evidence."""
    account = require_paper_account(app.managed_accounts)
    positions = app.get_positions()
    symbol = unused_symbol(symbols, positions, app.get_open_orders())
    bid, ask = checked_quote(app, symbol)
    entry_price = math.ceil(ask * 1.001 * 100) / 100
    if entry_price > max_notional:
        raise RuntimeError("One-share entry exceeds the probe notional cap.")
    stop = math.floor(bid * 0.99 * 100) / 100
    stop_limit = round(stop - 0.05, 2)
    if stop_limit <= 0:
        raise RuntimeError("Candidate price is too small for this probe.")
    # Recheck after quote acquisition; never adopt/cancel another strategy's work.
    unused_symbol([symbol], app.get_positions(), app.get_open_orders())
    require_trading_window()
    parent_id, child_id = app.nextOrderId(), app.nextOrderId()
    tag = "lumibot-paper-probe-" + uuid.uuid4().hex[:12]
    parent = NativeOrder()
    parent.orderId, parent.action, parent.orderType = parent_id, "BUY", "LMT"
    parent.totalQuantity, parent.lmtPrice = 1, entry_price
    parent.tif, parent.transmit, parent.account, parent.orderRef = "DAY", False, account, tag
    child = NativeOrder()
    child.orderId, child.action, child.orderType = child_id, "SELL", "STP LMT"
    child.totalQuantity, child.auxPrice, child.lmtPrice = 1, stop, stop_limit
    child.parentId, child.transmit = parent_id, True
    child.tif, child.account, child.orderRef = "DAY", account, tag
    for native in (parent, child):
        native.eTradeOnly = False
        native.firmQuoteOnly = False
    contract = Contract()
    contract.symbol, contract.secType, contract.exchange, contract.currency = symbol, "STK", "SMART", "USD"
    print(f"Paper verified; submitting tagged one-share OTO: {symbol}, parent={parent_id}, stop={child_id}", flush=True)
    # Do not use create_order(): this intentionally builds the exact native OTO
    # STOP_LIMIT topology, then exercises Strategy.modify_order on that child.
    app.placeOrder(parent_id, contract, parent)
    app.placeOrder(child_id, contract, child)
    wait_until(lambda: app.filled(parent_id), timeout, "one-share entry execution")

    def active_stop():
        orders = app.get_open_orders()
        if orders is None:
            raise RuntimeError("Open-order reconciliation unavailable.")
        matches = [native for native in orders if native.orderId == child_id]
        if not matches:
            return False
        native = matches[0]
        if (
            native.parentId != parent_id or native.clientId != app.client_id
            or native.account != account or native.orderRef != tag
            or native.orderType != "STP LMT" or native.action != "SELL"
            or native.totalQuantity != 1
        ):
            raise RuntimeError("Tagged protective child identity/topology mismatch.")
        return native.orderState.status in {"Submitted", "PreSubmitted"}

    wait_until(active_stop, timeout, "active protective STOP_LIMIT child")
    broker = InteractiveBrokers.__new__(InteractiveBrokers)
    broker.ib = app
    strategy = SimpleNamespace(broker=broker)
    order = Order("paper-probe", Asset(symbol), 1, "sell", stop_price=stop, stop_limit_price=stop_limit)
    order.identifier, order.status = child_id, "new"
    bid, ask = checked_quote(app, symbol)
    tightened = math.floor(bid * 0.995 * 100) / 100
    if tightened <= stop:
        raise RuntimeError("Market moved; cannot safely demonstrate a tighter stop.")
    if Strategy.modify_order(strategy, order, stop_price=tightened, limit_price=round(tightened - 0.05, 2)) is not order:
        raise RuntimeError("Modification returned no acknowledgement.")
    print(f"Same stop ID {child_id}: tightening acknowledged.", flush=True)
    bid, ask = checked_quote(app, symbol)
    # A marketable stop reprice may be rejected by TWS; that is a failed probe,
    # never permission to submit a second SELL or remove the existing stop.
    if Strategy.modify_order(
        strategy, order, stop_price=round(ask + 0.02, 2),
        limit_price=math.floor(bid * 0.995 * 100) / 100,
    ) is not order:
        raise RuntimeError("Exit reprice returned no acknowledgement.")
    wait_until(lambda: app.filled(child_id), timeout, "same-ID stop exit execution")

    def reconciled():
        positions, orders = app.get_positions(), app.get_open_orders()
        if positions is None or orders is None:
            raise RuntimeError("Final broker reconciliation unavailable.")
        flat = not any(p["symbol"] == symbol and p["position"] != 0 for p in positions)
        remaining = any(native.orderId in {parent_id, child_id} or native.orderRef == tag for native in orders)
        return flat and not remaining

    wait_until(reconciled, timeout, "flat account symbol and no tagged open orders")
    print(f"PASS: {symbol} flat; no probe orders; stop ID {child_id} retained throughout.", flush=True)
    return {"symbol": symbol, "parent_id": parent_id, "stop_id": child_id, "flat": True}


class PaperProbeApp(IBApp):
    """Minimal installed-wheel socket harness; deliberately no strategy worker threads."""

    def __init__(self, host, port, client_id, timeout):
        IBWrapper.__init__(self)
        IBClient.__init__(self, wrapper=self)
        self.client_id, self.subaccount, self.max_wait_time = client_id, None, timeout
        self.managed_accounts = []
        self.ready, self.accounts_ready = Event(), Event()
        self.executed = {}
        self._quote = {}
        self._quote_ready = Event()
        self.ib_broker = SimpleNamespace(on_status_event=lambda *args: None)
        self.init_error()
        self.connect(host, port, client_id)
        self._thread = Thread(target=self.run, daemon=True)
        self._thread.start()

    def await_ready(self):
        if not self.ready.wait(self.max_wait_time) or not self.accounts_ready.wait(self.max_wait_time):
            raise TimeoutError("Paper socket handshake/account verification timed out.")
        require_paper_account(self.managed_accounts)

    def nextValidId(self, order_id):
        super().nextValidId(order_id)
        self.ready.set()

    def managedAccounts(self, accounts):
        self.managed_accounts = accounts.split(",")
        self.accounts_ready.set()

    def execDetails(self, request_id, contract, execution):
        account = require_paper_account(self.managed_accounts)
        if execution.acctNumber == account and execution.clientId == self.client_id:
            self.executed[execution.orderId] = (float(execution.cumQty), execution.side)

    def filled(self, order_id):
        return self.executed.get(order_id, (0, ""))[0] == 1

    def _get_open_orders(self):
        storage = self.init_orders()
        self.reqAllOpenOrders()
        try:
            return storage.get(timeout=self.max_wait_time)
        except queue.Empty:
            raise TimeoutError("Paper open-order snapshot timed out.") from None

    def get_positions(self):
        storage = self.init_positions()
        self.reqPositions()
        try:
            return storage.get(timeout=self.max_wait_time)
        except queue.Empty:
            raise TimeoutError("Paper position snapshot timed out.") from None
        finally:
            self.cancelPositions()

    def quote_prices(self, symbol):
        self._quote = {}
        self._quote_ready.clear()
        self._quote_id = self.get_reqid()
        contract = Contract()
        contract.symbol, contract.secType, contract.exchange, contract.currency = symbol, "STK", "SMART", "USD"
        self.reqMarketDataType(1)
        self.reqMktData(self._quote_id, contract, "", False, False, [])
        try:
            if not self._quote_ready.wait(self.max_wait_time) or self._quote.get("type") != 1:
                raise TimeoutError("Fresh real-time paper bid/ask unavailable; no price substitution.")
            if time.monotonic() - min(self._quote["bid_at"], self._quote["ask_at"]) > 5:
                raise RuntimeError("Paper bid/ask is stale.")
            return self._quote["bid"], self._quote["ask"]
        finally:
            self.cancelMktData(self._quote_id)

    def marketDataType(self, reqId, marketDataType):
        if reqId == getattr(self, "_quote_id", None):
            self._quote["type"] = marketDataType
            self._signal_quote()

    def tickPrice(self, reqId, tickType, price, attrib):
        if reqId == getattr(self, "_quote_id", None) and tickType in {1, 2}:
            name = "bid" if tickType == 1 else "ask"
            self._quote[name], self._quote[name + "_at"] = price, time.monotonic()
            self._signal_quote()

    def _signal_quote(self):
        if {"bid", "ask", "type"} <= self._quote.keys():
            self._quote_ready.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-paper", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int)
    parser.add_argument("--symbols", nargs="+", default=["SPY", "IWM", "DIA"])
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--max-notional", type=float, default=1000)
    args = parser.parse_args(argv)
    if not args.execute_paper:
        print("NO ACTION: pass --execute-paper and a dedicated --client-id to permit the one-share paper probe.")
        return 0
    if (
        args.client_id is None or args.client_id <= 0 or not 1 <= args.port <= 65535
        or not math.isfinite(args.timeout) or not 0 < args.timeout <= 60
        or not math.isfinite(args.max_notional) or not 0 < args.max_notional <= 1000
    ):
        parser.error("Require nonzero dedicated client ID, valid port, timeout <=60s and notional <=1000.")
    previous_log_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)  # IB callbacks can otherwise log unmasked account identifiers.
    app = None
    try:
        require_trading_window()
        app = PaperProbeApp(args.host, args.port, args.client_id, args.timeout)
        app.await_ready()
        run_probe(app, args.symbols, args.timeout, args.max_notional)
        return 0
    except Exception as exc:
        # Do not print SDK messages/tracebacks containing account or connection data.
        code = getattr(exc, "error_code", None)
        code_text = f", code={code}" if type(code) is int else ""
        print(f"FAIL ({type(exc).__name__}{code_text}): no retry/replacement/automatic cancellation. "
              "Reconcile the printed tagged order IDs in paper TWS; protection may remain.", file=sys.stderr)
        return 1
    finally:
        if app is not None:
            app.disconnect()
            app._thread.join(timeout=args.timeout)
        logging.disable(previous_log_disable)


if __name__ == "__main__":
    raise SystemExit(main())
