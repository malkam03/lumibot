# Upstream Fix Reconciliation

Records the local fork fixes compared against canonical upstream.

Last Updated: 2026-10-05

Status: Active

Audience: LumiBot maintainers and AI coding agents

## Overview

This document records the reconciliation status for commits currently carried on
the local fork branch relative to canonical `upstream/dev`. It distinguishes
duplicate upstream work from novel fork fixes so future agents know what should
be merged upstream, left local, or coordinated with an existing PR.

Canonical upstream: `Lumiwealth/lumibot`

Fork branch inspected: `malkam03-dev`

Canonical branch compared for fork issue #7: `upstream/dev` at `4280719f`
(`4.6.4`, merged into `malkam03-dev` as `c4975b2c` on 2026-10-05).
Older reconciliation rows retain their original comparison dates and evidence.

## Reconciliation Table

| Commit | Fix | Classification | Upstream evidence | Local action | Upstream plan |
|---|---|---|---|---|---|
| Pending (fork issue #7) | Keep a received IBKR LAST tick ahead of the previous-close fallback, independent of arrival order. | Fork bug fix; absent from inspected upstream. | `upstream/dev` at `4280719f` still guards CLOSE with `self.tick is None`, which never records LAST receipt. | Use `tick_type_used != 4`; retain enabled close fallback when no LAST arrives. Offline snapshot tests cover both arrival orders, fallback enabled/disabled, request reset, and bid/ask preservation. | Submit through a fork PR targeting `malkam03-dev`; no upstream PR requested. |
| `4273e288` | Preserve Yahoo intraday bar timestamps instead of stamping every intraday bar to the daily close. | Exact duplicate of an open upstream PR. | Lumiwealth/lumibot#1163 has the same author, patch, tests, and commit content. As of this record, #1163 is open and review-required, not merged. | Keep provenance clear; do not create a competing PR from this branch for the same fix. | Let #1163 merge, or coordinate with the PR author if it stalls. |
| `146e4171` | Fetch retained Yahoo 1-minute history in seven-day windows and combine real returned bars. | Novel LumiBot adaptation for a known Yahoo/yfinance limitation. | ranaroussi/yfinance#356 documents that Yahoo retains roughly one month of 1-minute data but limits each request to seven days; ranaroussi/yfinance#959 only fixed `period="max"` to one week and does not provide LumiBot multi-window aggregation. No matching LumiBot PR was found. | Keep as an independently cherry-pickable fix with tests and docs. | Candidate for its own upstream PR or direct merge after validation and review. |
| `9a0f8652` | Use minute-granularity timeshift for Yahoo intraday fills while preserving one-day timeshift for Yahoo daily fills. | Novel LumiBot bug fix. | No matching LumiBot issue or PR was found. `git blame` shows Yahoo fill timeshift was globally set to `-1 day`, while newer Yahoo paths can request minute data and need a minute-level fill lookup. | Add/keep a regression test that proves Yahoo minute fills request `timedelta(minutes=-1)` and day fills keep `timedelta(days=-1)`. | Candidate for its own upstream PR or direct merge after validation and review. |
| `e3a7b38f`..`5c6af4e4` (fork PR #3, merge `94ab3286`) | Add `InteractiveBrokersTWSBacktesting`, a backtesting data source that reads history from a user-run IB Gateway/TWS over the `ibapi` socket, with its cache, config, docs, and tests. | Novel fork feature. | No matching LumiBot data source or PR was found; upstream's IBKR backtesting uses the hosted REST/Data Downloader path. | Keep on `malkam03-dev`; entered the local integration branch via merge `8eca4801`. | Candidate for its own upstream PR: cherry-pick `e3a7b38f`..`5c6af4e4` (not the merge commit) onto a branch from current `upstream/dev`. |
| `103ef76b` | Add the upstream-fix reconciliation skill. | Local workflow documentation, not a runtime fix. | Not searched as a product bug; this supports fork maintenance workflow. | Keep local unless maintainers explicitly want the skill upstreamed. | No upstream merge planned by default. |

## Upstream Merge Status

`upstream/dev` was merged into `malkam03-dev` on 2026-09-28, bringing the fork
from `4.6.0` to `4.6.2` (55 commits, merge commit `93b3a755`). The merge was
conflict-free: upstream modified none of the files the fork's fixes patch.

Verification at merge time:

- Full unit suite (`-m "not apitest and not downloader"`, excluding
  `tests/backtest/`) passed before and after the merge: 3049 → 3159 passed,
  zero failures. The increase is upstream's own new tests.
- `9a0f8652`'s minute-granularity timeshift survived the merge intact, and its
  regression test in `tests/test_market_infinite_loop_bug.py` passes (committed
  as `8bf50265`).
- Upstream's new intraday lookahead work (`be0df267`, `16ed3345`) lives in
  `lumibot/entities/data.py` and guards only on `timeshift >= 0`. The fork's
  Yahoo fix passes negative timeshifts through
  `YahooData._get_filtered_end_index()` in `lumibot/data_sources/yahoo_data.py`,
  a separate code path, so the two do not double-apply.
- Lumiwealth/lumibot#1163 (matching `4273e288`) remains OPEN, and
  `lumibot/tools/yahoo_helper.py` still lacks `146e4171`'s chunked minute
  fetching upstream. Both rows above stand unchanged.

`origin/malkam03-dev` (fork PR #3, IBKR TWS backtesting) was merged into the
local `malkam03-dev` on 2026-09-29 as `8eca4801`. The only conflict was
`CHANGELOG.md`, resolved by keeping the fork's `Unreleased` section above
upstream's `4.6.2` and `4.6.1` entries. The full unit suite
(`-m "not apitest and not downloader"`, excluding `tests/backtest/`) passed
on the merged tree: 3307 passed, zero failures.
