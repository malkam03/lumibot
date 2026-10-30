# IBKR Socket Order Modification

Same-ID, acknowledged price changes for the legacy TWS/Gateway socket adapter.

Last Updated: 2026-10-29
Status: Offline regression coverage; deployed paper lifecycle qualification pending
Audience: Contributors and operators

## Overview

The candidate includes the deployed 4.6.4 framework base, including client-scoped
identity, LAST-tick precedence and staged bracket/OTO submission acknowledgement.
Modification callbacks coexist with that submission machinery; duplicate-ID
modification rejections never allocate a new ID or use submission retry logic.

The socket adapter supports positive, finite price changes on active native
`LMT`, `STP` and `STP LMT` orders. This addresses
[issue #12](https://github.com/malkam03/lumibot/issues/12). The REST adapter is
unchanged. Accuracy is qualified against the real broker lifecycle, not just
synthetic callback tests: offline success is not a claim that the deployed
customer path is fixed.

## Request and result contract

`Strategy.modify_order(order, limit_price=..., stop_price=...)` delegates to the
adapter. It returns the **same LumiBot Order instance** only after confirmation.
For a stop-limit order, `limit_price` changes native `lmtPrice`; successful calls
update both local `limit_price` and `stop_limit_price`. `stop_price` changes
native `auxPrice`.

The adapter reads the broker's open-order snapshot, verifies native client,
account, asset/contract identity, side, quantity and active state, and deep-copies
that native order. It sends `placeOrder` with the **existing ID**, changing only
the requested prices. It never uses the submission builder, allocates a new ID,
changes quantity or reconstructs parent/transmit/OCA/account fields. A request
already satisfied by the fresh snapshot returns without sending.

Only a matching `openOrder` callback with `Submitted` or `PreSubmitted` state,
requested prices and matching identity/topology confirms a change. A
permanent-ID mismatch cannot acknowledge a request. Snapshot uniqueness is
scoped to the connected client as well as the numeric order ID, so another
client's same-numbered order cannot shadow the owned order. A generic
`orderStatus("Submitted")`, an old-price callback or an informational warning
does not. Snapshot calls and modifications are serialized per connection, while
callback delivery uses a separate lock. Blocking modification from the socket
callback thread is rejected.

| Outcome | Result | Caller action |
|---------|--------|---------------|
| Confirmed | Original Order | Commit the strategy's stop state |
| Explicit rejection | `IBOrderModificationRejected`, a `RuntimeError`, with `order_id` and `error_code` | Record failure; do not claim new prices |
| Timeout, disconnect, send uncertainty, unclassified error, or terminal transition without price confirmation | `IBOrderModificationUnknown`, a `TimeoutError`, with `order_id` | Freeze the exit workflow and reconcile orders, executions and position |
| Invalid/inactive/foreign/ambiguous order or unsupported field/type | `ValueError`, before modification send | Correct the request; do not replace protection |
| Disconnected or unavailable snapshot | `ConnectionError`, before modification send | Reconcile connectivity/state |

Neither rejection nor unknown changes local prices. Unknown IDs are quarantined
for the lifetime of the connection, even if a late callback arrives. There is
no automatic retry, cancellation, replacement sell or automatic quarantine
reset. After manual reconciliation, a newly established connection can be used
for a deliberate next action; reconnecting alone is **not** reconciliation.
The existing generic Strategy inactive-order short circuit still returns `None`;
callers requiring acknowledgement must check `result is order`.

The built-in SmartLimit executor recognizes the exception's
`requires_reconciliation` marker and sets the order's serialized
`modification_requires_reconciliation` latch. It retains the previous local
price and blocks subsequent repricing, cancellation/replacement and final-hold
cancellation, including after an order serialization round trip. It logs the
unknown outcome once instead of silently abandoning the ladder. Run-once
execution remains unresolved and reaches its bounded drain failure rather
than reporting a clean completion. The latch must not be cleared until broker
orders, executions and positions have been explicitly reconciled; there is no
automatic recovery. Other brokers' existing modification/fallback behavior is
unchanged.

IBKR has no modification-request ID/sequence in `openOrder`. Matching broker
state is the confirmation evidence, not proof of exchange execution or a fill
guarantee. A STOP_LIMIT can remain unfilled after triggering. Broker warnings
are not treated as explicit rejection, and unknown errors are intentionally
conservative.

## Offline validation

Run without credentials or TWS:

```bash
LUMIBOT_DISABLE_DOTENV=1 python -m pytest \
  tests/test_ibkr_socket_modification.py \
  tests/test_ibkr_paper_modification_probe.py -q
```

## Authorized deployed paper probe

`scripts/ibkr_paper_modification_probe.py` runs against the installed package,
not an overlaid adapter file. Build/install the exact committed wheel through
the owning deployment gates before qualification. The script does not deploy.
Its default invocation opens **no connection**:

```bash
python scripts/ibkr_paper_modification_probe.py
```

Only after explicit paper-test authorization, use a dedicated unused nonzero
client ID and the configured paper socket:

```bash
python scripts/ibkr_paper_modification_probe.py \
  --execute-paper --host <configured-paper-host> \
  --port <configured-paper-port> --client-id <unused-client-id>
```

The script requires exactly one managed paper account matching
`DU[A-Z]?\d+`; it ignores blank comma-delimited callback entries, but rejects
non-string identities. It does not infer paper identity from a port. It
excludes symbols with any visible position or open order, rechecks exposure
before sending, requires fresh real-time bid/ask,
caps the order at one share and $1,000 notional, and refuses closed sessions or
the final 15 minutes (including early closes). Other sessions must not trade
the selected symbol during the probe; the API cannot atomically reserve it.
Candidate and broker symbols are canonicalized for every exposure check and
final position reconciliation, including lowercase/whitespace and class-share
aliases; the selected contract uses IBKR's native symbol convention.

It submits a tagged native limit BUY plus a transmitted protective STOP_LIMIT
child, waits for entry execution and child acknowledgement, then calls the real
`Strategy.modify_order` path to tighten that same child. It attempts a
marketable same-ID stop reprice to exit, waits for the execution and checks
fresh positions/orders for a flat selected symbol and no probe orders.

Native submission is intentional: this isolates modification from legacy OTO
builder behavior. It is not proof of the complete saved strategy/deployed
artifact path. That exact customer lifecycle must be qualified separately.

TWS may reject a marketable stop reprice. Any failure exits nonzero, without
retrying, cancelling protection or sending an alternative sell. Tagged orders
or exposure **may remain**, including an untransmitted parent after a partial
submission. Reconcile the printed IDs in paper TWS before any next action.
Normal success requires confirmed flat/no-test-order reconciliation; failure
does not pretend cleanup succeeded. Account identifiers, endpoints and raw SDK
errors are not printed.
