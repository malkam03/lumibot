# IBKR Socket Order Identity

Ownership and duplicate-ID handling for independent TWS/IB Gateway clients.

Last Updated: 2026-10-20
Status: Implemented; network-free regression coverage, live qualification pending
Audience: Broker maintainers and strategy integrators

## Overview

The socket adapter treats `(client ID, order ID)` as order identity, not the
numeric order ID alone. Snapshots, direct lookups, parsing, status/execution
callbacks and cancellation reject foreign-client orders. A configured subaccount
also filters native snapshots and executions. Positions and balances remain
account-scoped; this change does not partition shared account holdings.

Parsed orders retain `broker_client_id`, `broker_perm_id` and `broker_account`.
Submitted parents and native children carry client/account metadata. Both broker
and executor reconciliation use adapter identity hooks; known conflicting
permanent IDs, accounts or assets cannot overwrite local orders. Derivative
snapshots use the submitted contract root symbol, with expiration/strike/right
retained to distinguish contracts whose local symbols include broker-specific
codes. Duplicate cleanup removes only entries verified against the same order
identity, including when local objects compare equal by numeric order ID. Older
local orders lacking client metadata are treated as belonging to the configured
client, never as permission to adopt a foreign snapshot. Other adapters keep
numeric/string identifier matching.

## Allocation and submission

`IBWrapper.nextOrderId(timeout=10)` serializes allocation, clears the previous
reply notification, sends `reqIds(-1)` and waits for a new `nextValidId`.
It returns at least `max(reply, last allocated + 1)` and exceeds any order IDs
observed through open-order/status callbacks, including foreign clients.
A missing reply raises
`TimeoutError`, never falls back to the connection-time counter. There are no
fixed client-specific ranges and no new environment variables.

`IBApp.execute_order(orders, timeout=10)` serializes submissions within one
connection. Native legs are tracked before placement and wait for an open-order,
status or execution acknowledgement. A confirmed error 103 retries that native
leg once with a fresh ID. Tracked identifiers, unsent leg identifiers and parent
links are updated together; accepted legs are not resubmitted. Other rejection
codes are correlated only for documented order-specific IBKR error codes; an
unrelated request error with a numerically matching request ID does not reject
the order. For non-transmitting bracket/OTO legs, `PendingSubmit` confirms that
the leg is staged and allows the next leg to be sent, but the pending submission
remains tracked until IBKR reports final acceptance or rejection. Only
Submitted, PreSubmitted, or Filled confirms final acceptance through either
openOrder or orderStatus. A second 103 for that leg produces an error event and
raises `RuntimeError`. IBKR informational warnings are not rejection
acknowledgements.

An acknowledgement timeout raises `TimeoutError` with **unknown submission
outcome** and does not retry: the broker might already have accepted or filled
the order. Reconcile against IBKR before taking further action. If a multi-leg
submission stops partway through, inspect already accepted/staged legs and
reconcile them; do not blindly replay the graph. Terminal callback status is
not reset to submitted on return from placement.

`reqIds` is not a distributed reservation or lock across processes. A fresh
reply reduces stale allocations but cannot guarantee collision-free placement
between independent clients. One bounded retry is intentionally not an
unlimited retry loop. Call allocation/submission from application threads,
never from the IB API reader callback thread, which must deliver the replies.

## Integration and qualification

Remove downstream fixed-range allocator monkey patches to use this behavior.
Public strategy method signatures and numeric `Order.identifier` are unchanged.
The optional timeout arguments above belong to the socket transport helpers.
An incomplete native order snapshot raises instead of masquerading as empty;
executor synchronization releases held trade events even when refresh fails.

Offline coverage lives in `tests/test_ibkr_order_identity.py` and
`tests/test_interactive_brokers_advanced_orders.py`. It covers foreign orders with
equal/different IDs, terminal preservation, partial-fill callbacks, both sync
paths, metadata conflicts, concurrent allocation, missing replies, bounded
rejection retries and bracket parent/child rekeying. These tests establish
adapter behavior, not live multi-client Gateway qualification.
