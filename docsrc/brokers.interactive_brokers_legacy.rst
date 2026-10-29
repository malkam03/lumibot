Interactive Brokers Legacy
===================================

.. meta::
   :description: This method has been deprecated. Please use the other Interactive Brokers tutorial. LumiBot documentation.

.. note::
    This method has been deprecated. Please use the other Interactive Brokers tutorial.

Once installed, navigate in Trader Workstation to ``File -> Global Configuration -> API -> Settings`` The key settings required to trade using Lumibot are:

#. Enable ActiveX and Socket Clients
#. Disable Read-Only API
#. Socket port ``7496`` for live trading, ``7497`` for paper account trading.
    * NOTE: It is highly recommended to thoroughly test your algorithm in paper trading mode before trading live.
#. Master API Client ID: You can find in the Trader Workstation by going to ``File -> Global Configurations -> API -> Settings``, then looking for "Master API client ID". This can be any number you choose up to ``999``. You will use this in your configuration file to log in.

.. image:: interactive_brokers.png

Set up your credentials as follows in ``credentials.py``:

.. code-block:: python

    INTERACTIVE_BROKERS_CONFIG = {
        "SOCKET_PORT": 7497,
        "CLIENT_ID": "your Master API Client ID three digit number",
        "IP": "127.0.0.1",
    }

Set up your entry point file as above, except using Interactive Brokers. Here is an example of a completed file:

.. code-block:: python
    :emphasize-lines: 3,12

    from lumibot.traders import Trader
    # Import interactive brokers
    from lumibot.brokers import InteractiveBrokers
    from lumibot.strategies.examples import Strangle
    from credentials import INTERACTIVE_BROKERS_CONFIG


    trader = Trader()
    # Initialize interactive brokers
    interactive_brokers = InteractiveBrokers(INTERACTIVE_BROKERS_CONFIG)

    strategy = Strangle(broker=interactive_brokers)
    trader.add_strategy(strategy)
    trader.run_all()

You can also see the file simple_start_ib.py for a working bot here: https://github.com/Lumiwealth/lumibot/blob/master/getting_started/simple_start_ib.py

Modifying active socket orders
-----------------------------------

The socket adapter supports changing positive, finite prices on active limit,
stop and stop-limit orders owned by the connected API client. It preserves the
existing broker order ID, quantity, parent linkage, transmit setting and other
native order fields. It does not cancel and replace the order.

.. code-block:: python

    # For an existing protective STOP_LIMIT child:
    confirmed = self.modify_order(child_order, stop_price=95.00, limit_price=94.95)
    if confirmed is not child_order:
        raise RuntimeError("Order modification was not acknowledged")

For stop-limit orders, ``limit_price`` changes the stop's limit and updates both
``order.limit_price`` and ``order.stop_limit_price`` after confirmation. A stop
order accepts only ``stop_price``; a limit order accepts only ``limit_price``.
Quantity changes, trailing stops and other order types are not supported by this
modification path.

A matching broker ``openOrder`` acknowledgement is required before returning the
original order and updating its local prices. An already-satisfied request is
confirmed using a fresh broker snapshot without another send.
``IBOrderModificationRejected`` (a ``RuntimeError``) exposes ``order_id`` and
``error_code`` for an explicit broker rejection.
``IBOrderModificationUnknown`` (a ``TimeoutError``) exposes ``order_id`` when
delivery or confirmation is uncertain. Both are importable from
``lumibot.brokers.interactive_brokers``. Neither exception updates local prices.
Invalid, inactive or foreign-client native orders raise ``ValueError`` before
sending; missing connectivity or snapshots raise ``ConnectionError``.

.. warning::

    On an unknown outcome, do not retry, cancel protection or submit a
    replacement sell. Reconcile broker orders, executions and positions first.
    That order ID remains blocked from further modification on the same
    connection, even after a late callback. Reconnecting is not reconciliation.
    Acceptance is not a fill guarantee: a triggered stop-limit may remain
    unfilled. The generic strategy method's existing inactive-order short
    circuit returns ``None`` rather than acknowledgement.