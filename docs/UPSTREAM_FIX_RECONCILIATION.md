# Upstream Fix Reconciliation

Records the local fork fixes compared against canonical upstream.

Last Updated: 2026-09-28

Status: Active

Audience: LumiBot maintainers and AI coding agents

## Overview

This document records the reconciliation status for commits currently carried on
the local fork branch relative to canonical `upstream/dev`. It distinguishes
duplicate upstream work from novel fork fixes so future agents know what should
be merged upstream, left local, or coordinated with an existing PR.

Canonical upstream: `Lumiwealth/lumibot`

Fork branch inspected: `malkam03-dev`

Canonical branch compared: `upstream/dev` (merged through `4.6.2`)

## Reconciliation Table

| Commit | Fix | Classification | Upstream evidence | Local action | Upstream plan |
|---|---|---|---|---|---|
| `4273e288` | Preserve Yahoo intraday bar timestamps instead of stamping every intraday bar to the daily close. | Exact duplicate of an open upstream PR. | Lumiwealth/lumibot#1163 has the same author, patch, tests, and commit content. As of this record, #1163 is open and review-required, not merged. | Keep provenance clear; do not create a competing PR from this branch for the same fix. | Let #1163 merge, or coordinate with the PR author if it stalls. |
| `146e4171` | Fetch retained Yahoo 1-minute history in seven-day windows and combine real returned bars. | Novel LumiBot adaptation for a known Yahoo/yfinance limitation. | ranaroussi/yfinance#356 documents that Yahoo retains roughly one month of 1-minute data but limits each request to seven days; ranaroussi/yfinance#959 only fixed `period="max"` to one week and does not provide LumiBot multi-window aggregation. No matching LumiBot PR was found. | Keep as an independently cherry-pickable fix with tests and docs. | Candidate for its own upstream PR or direct merge after validation and review. |
| `9a0f8652` | Use minute-granularity timeshift for Yahoo intraday fills while preserving one-day timeshift for Yahoo daily fills. | Novel LumiBot bug fix. | No matching LumiBot issue or PR was found. `git blame` shows Yahoo fill timeshift was globally set to `-1 day`, while newer Yahoo paths can request minute data and need a minute-level fill lookup. | Add/keep a regression test that proves Yahoo minute fills request `timedelta(minutes=-1)` and day fills keep `timedelta(days=-1)`. | Candidate for its own upstream PR or direct merge after validation and review. |
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
