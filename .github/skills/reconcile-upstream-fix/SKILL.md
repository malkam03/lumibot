---
name: reconcile-upstream-fix
description: "Research whether a local fix is already known upstream, has an open or merged PR, or duplicates a dependency bug; then test, review, and create clean cherry-pickable commits. Use when inspecting local patches, deciding whether to cherry-pick an upstream fix, preparing a fix for an upstream PR, or maintaining a fork integration branch."
---

# Reconcile a local fix with upstream

Use this workflow when a repository contains one or more local fixes and the
user wants to know whether they are known upstream before committing them.

The goal is not merely to find similar text. Establish whether each local
change is:

1. an exact duplicate of an open upstream PR;
2. already merged upstream;
3. partially overlapping an upstream change;
4. a known limitation in an external dependency;
5. or novel work that should receive its own tests, documentation, and commit.

## Required outcome

For every independent fix:

- identify its root cause and user-visible behavior;
- cite matching upstream issues, PRs, commits, or dependency reports;
- compare the actual implementations and tests;
- maintain a reconciliation table as evidence is gathered, not only at the end;
- retain provenance when reusing upstream work;
- validate the final code;
- run a meaningful rubber-duck review before committing novel work;
- create one independently cherry-pickable commit per fix;
- report the commit SHA and upstream relationship.

Do not create or update an upstream pull request unless the user explicitly
requests that action.

## 1. Establish repository and workspace state

Read the repository instructions before touching code. Then inspect:

```bash
git status --short
git branch --show-current
git remote -v
git branch -vv
git worktree list --porcelain
```

Never assume `origin` is the canonical upstream. Identify:

- the canonical project remote, commonly `upstream`;
- the user's fork, commonly `origin`;
- the branch tracking canonical development;
- the fork integration branch carrying approved patches.

Uncommitted changes in another worktree are not visible. Do not inspect or
modify another checkout unless the workspace rules allow it. Ask the user to
export a patch when necessary:

```bash
git diff --binary > "$TMPDIR/local-fixes.patch"
```

Read exported patches without applying them first.

## 2. Split the patch into independent fixes

Do not treat a multi-hunk patch as one bug merely because it touches one file.
For each behavior change, record:

- affected file and function;
- old behavior;
- intended behavior;
- trigger conditions;
- likely regression origin from `git blame` and file history;
- whether it is a bug fix or a capability expansion.

Search local history before searching GitHub:

```bash
git log --all --oneline -- path/to/file
git blame -L START,END -- path/to/file
```

Keep unrelated fixes in separate commits.

## 3. Search canonical upstream

Search open and closed issues and pull requests using several symptom-oriented
queries. Do not rely on one exact phrase.

```bash
gh search issues "<symptom>" --repo OWNER/REPO --state open
gh search issues "<symptom>" --repo OWNER/REPO --state closed
gh search prs "<symptom>" --repo OWNER/REPO
gh search prs "<function-or-file>" --repo OWNER/REPO
```

Also inspect:

- the current canonical development version of the affected file;
- recent commits touching that file;
- candidate PR bodies, files, commits, reviews, and comments;
- closed or superseded PRs named by the candidate;
- release notes when a fix may already have shipped.

Useful commands:

```bash
gh pr view NUMBER --repo OWNER/REPO \
  --json number,title,state,isDraft,mergeable,reviewDecision,createdAt,updatedAt,body,commits,files
gh pr diff NUMBER --repo OWNER/REPO --patch
gh api "repos/OWNER/REPO/commits?path=PATH&sha=BRANCH&per_page=30"
gh api "repos/OWNER/REPO/contents/PATH?ref=BRANCH"
```

When timing matters, query the PR timeline. Creation time is not necessarily
the time review was requested.

## 4. Search dependency upstream when appropriate

If the local code works around a provider or library restriction, search that
dependency's official documentation, issues, and source too.

Examples include:

- API retention or request-window limits;
- timestamp semantics;
- start/end inclusivity;
- retry or rate-limit behavior;
- model/provider compatibility.

Distinguish:

- a dependency limitation;
- a dependency bug;
- a missing adaptation in the current repository.

Do not call a new capability a repository bug when the existing API never
promised it.

## 5. Classify and choose the integration strategy

Keep a working reconciliation table and update it whenever evidence changes the
classification or upstream plan. Use this shape so it can be copied directly
into handoffs, release notes, or the final report:

| Commit | Fix | Classification | Upstream evidence | Local action | Upstream plan |
|---|---|---|---|---|---|

If the user asks for a persistent record, write the table to the repository's
normal docs or handoff location and keep it current through the rest of the
workflow. Otherwise, keep it in a session artifact and include the final version
in the response.

### Exact open upstream PR

- Compare code and tests line by line.
- Prefer the upstream implementation if it is at least as correct.
- Note review state and unresolved findings.
- With user approval, fetch and cherry-pick the upstream commit as its own
  commit so original authorship and message are preserved.
- Do not open a competing duplicate PR without coordinating with its author.

### Already merged upstream

- Synchronize from canonical upstream instead of recreating the fix.
- Do not add a duplicate commit.

### Partial overlap

- Reuse the correct upstream portion.
- Add only the missing behavior in a separate follow-up commit.
- Make dependencies between commits explicit.

### Known dependency limitation but no repository fix

- Treat the work as a repository enhancement.
- Cite the dependency evidence.
- Add repository-level tests for the adaptation.

### Novel fix

- Implement the smallest complete correction.
- Add regression tests and directly related documentation.
- Update the changelog when required by repository policy.

## 6. Test authority and coverage

Read scoped test instructions before modifying tests. Check test age when an
existing expectation must change. Old tests normally outrank a new patch.

New behavior requires deterministic unit or regression tests covering:

- the exact reported failure;
- normal behavior that must remain unchanged;
- boundaries and malformed inputs;
- partial and total provider failures;
- retry and rate-limit behavior;
- integration through the public caller, not only a new helper;
- state leakage and global caches;
- ordering, deduplication, timezone, and schema invariants where relevant.

Never make a live provider call the only proof of correctness.

## 7. Run a rubber-duck review before novel commits

After tests exist and before committing novel work, invoke a rubber-duck review
with complete context. Ask it to inspect:

- root-cause correctness;
- provider/API boundary semantics;
- retry amplification and idempotency;
- partial-data handling;
- global-state poisoning;
- caching and rate limits;
- missing integration tests;
- documentation overclaims;
- compatibility with neighboring implementations.

Address blocking, high-confidence findings. If declining a finding, document
the technical reason.

## 8. Validate with repository tooling

Install only the repository-declared development dependencies. Run the
smallest test and lint commands that cover the change, then broaden when the
results require it.

At minimum:

```bash
pytest <targeted tests>
ruff check <changed Python files>
git diff --check
```

Separate new violations from pre-existing file-wide violations. Do not perform
unrelated cleanup merely to make a touched legacy file globally clean.

Build directly affected documentation when repository policy requires it.

## 9. Commit structure and branch handling

Before every commit:

```bash
git branch --show-current
git status --short
git diff --check
git diff --cached
```

Rules:

- one independent fix per commit;
- preserve upstream authorship via cherry-pick for reused work;
- put tests and directly related docs in the same novel-fix commit;
- use a message describing behavior, not the investigation process;
- never amend without explicit permission;
- never switch or rewrite sensitive shared branches without explicit approval;
- do not push, merge, or open a PR unless requested.

For fork maintenance, prefer:

```text
canonical dev   -> tracks canonical upstream
fork dev        -> canonical dev plus approved cherry-pickable fixes
topic/session   -> isolated investigation and implementation
```

When merging into the fork branch, first incorporate any commits that appeared
there concurrently. Never overwrite the branch or force-push.

## 10. Final report

Lead with a table:

| Fix | Classification | Upstream evidence | Local action | Commit |
|---|---|---|---|---|

Then state:

- validation commands and results;
- unresolved review findings;
- whether the commit is safe to cherry-pick independently;
- whether an upstream PR already exists;
- the clean next action, if one is required.

Do not claim that a PR is reviewed merely because a bot commented. Distinguish
automated review from human approval.
