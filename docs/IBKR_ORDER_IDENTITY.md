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
connection. Native legs are split into graphs, each ending at a `transmit=True`
leg (a bracket/OTO is one graph; OCO peers, simple and multileg orders are
single-leg graphs). Every leg of a graph is tracked and placed back-to-back,
then one shared deadline covers the whole graph. Staged `transmit=False` legs
are not waited on individually: IB Gateway normally sends no callback for them
until the transmitting leg arrives. PR #10 waited on each staged parent for
`PendingSubmit`, which live Gateways do not send, so every native bracket timed
out before its children were placed (unknown outcome, never transmitted).

Only Submitted, PreSubmitted or Filled (openOrder, orderStatus or execution)
confirms acceptance; `PendingSubmit` is ignored. Rejections are correlated only
for documented order-specific IBKR error codes; an unrelated request error
with a numerically matching request ID does not reject the order, and IBKR
informational warnings are not acknowledgements. A leg is classified as:

- accepted, rejected (with code/message), or
- not transmitted: a staged leg without callbacks when the transmitting leg was
  never sent (placement stopped after an earlier rejection), or
- unknown: any other unconfirmed leg. Once the transmitting leg was sent, a
  silent staged leg stays unknown even if the transmitting leg itself was
  rejected, because that rejection does not prove the parent or earlier
  siblings never went live.

Graph outcome: all accepted succeeds; any unknown leg raises
`IBKROrderSubmissionUnknown` (also `TimeoutError`); rejections with any
accepted leg raise PARTIAL; otherwise REJECTED. Error events are recorded only
for rejected legs of a PARTIAL graph, and for every tracked leg of a REJECTED
graph. All are `IBKROrderSubmissionError` (`RuntimeError`) exposing client ID,
account, raw code/message and per-leg `IBKRSubmissionLeg` outcomes. A parent
rejection alone never implies the children are absent.

A call that submits several graphs (OCO peers) reports the whole call: if a
later graph fails after an earlier graph was accepted, the raised error
includes the accepted legs and is PARTIAL (or UNKNOWN when the failing graph is
unknown), chained from the per-graph error. It is never REJECTED while an
earlier peer is live.

Error 103 retries at most once per graph, only if the first rejected leg's
code is 103, the transmitting leg is rejected or unsent, and no leg at or after
the rejected leg was accepted. Legs from the rejected one onward are rekeyed
together, with parent links and tracked identifiers remapped; earlier staged
legs keep their IDs. A failed fresh-ID request during the retry raises
REJECTED chained from the `TimeoutError`. Staged legs are never cancelled.
A rejection reported for an early leg stops placement before the transmitting
leg is sent. Once the transmitting leg was sent, a 103 retry happens only if
every leg was explicitly answered; any rejection with silent staged legs
yields UNKNOWN (fail-closed, no retry).

An unknown outcome does not retry: the broker might already have accepted or
filled the order. Reconcile against IBKR before taking further action.
Terminal callback status is not reset to submitted on return from placement.

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
rejection retries, bracket parent/child rekeying, staged graph legs without
callbacks, delayed/missing/partial graph acknowledgement and typed errors.
These tests establish
adapter behavior, not live multi-client Gateway qualification.

For rollout qualification of an installed package (no test tree, no socket,
no orders), run `python scripts/ibkr_staged_bracket_smoke.py` with the target
interpreter. A fake gateway keeps staged legs silent and acknowledges the graph
only after its transmitting leg; the script checks leg IDs, transmit flags,
parent links, client identity and typed UNKNOWN on a missing acknowledgement.
It exits non-zero on failure (including when the typed API is absent).
