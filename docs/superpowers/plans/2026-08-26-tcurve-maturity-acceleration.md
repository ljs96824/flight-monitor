# T Curve Maturity Acceleration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct observation-day semantics, add request outcome and price-history lineage, then introduce a disabled-by-default six-slot hybrid research cohort without spending real API quota during development.

**Architecture:** Keep the existing append-only observations table and two-phase collection plan. Add timestamp and ledger migrations as backward-compatible columns/tables, make T-curve readers prefer canonical Shanghai observation days, and attach collection outcomes at the existing plan/cache boundary. Only after role-aware consumers exist, add a versioned basket cohort whose probes rotate on ledger-derived valid cells.

**Tech Stack:** Python 3.13, SQLite, standard-library `zoneinfo`, unittest/pytest, existing collection planner and runtime backup tooling.

---

### Task 1: Canonical observation timestamps

**Files:**
- Create: `observation_time.py`
- Create: `scripts/migrate_observation_timestamps.py`
- Create: `test_observation_timestamps.py`
- Modify: `observations_store.py`
- Modify: `tcurve.py`
- Modify: `method_registry.py`
- Modify: `test_observations_store.py`
- Modify: `test_tcurve.py`
- Create: `docs/observation-time-migration-2026-08-26.md`

- [ ] Write failing tests for UTC-to-Shanghai day conversion, date-boundary conversion, daylight-saving offsets, schema columns, explicit legacy classification, ambiguous-row exclusion, and method versions.
- [ ] Run the targeted tests and record expected failures caused by missing columns/functions and v1 versions.
- [ ] Implement canonical conversion through `project_time.SHANGHAI_TZ`, preserving `observed_at` compatibility while writing `observed_at_utc`, `observed_day_shanghai`, and `legacy_time_ambiguous`.
- [ ] Implement a dry-run-first historical migration; naive values require the explicit `--assume-naive-shanghai` flag before write.
- [ ] Make T-curve reads prefer the canonical day, use the audited legacy fallback only for unmarked rows, and exclude marked ambiguous rows.
- [ ] Audit the production database read-only, document classification counts and T-cell differences, then run the explicit migration only after tests pass.
- [ ] Run targeted and full regressions, frozen email, PushPlus, fixtures, snapshots, and production-state hash checks.
- [ ] Commit as `fix: canonicalize observation timestamps`, push, and wait for the four CI cells.

### Task 2: Collection outcome ledger and round lineage

**Files:**
- Create: `collection_ledger.py`
- Create: `scripts/migrate_collection_lineage.py`
- Create: `test_collection_ledger.py`
- Create: `test_storage_round_lineage.py`
- Modify: `collection_plan.py`
- Modify: `request_cache.py`
- Modify: `observations_store.py`
- Modify: `storage.py`
- Modify: `forecast.py`
- Modify: `method_registry.py`
- Modify: collection entry points in `main.py` and `basket_collect.py`
- Create: `docs/collection-ledger-and-lineage-2026-08-26.md`

- [ ] Write failing migration tests for the `collection_cells` schema and the three `prices.db` `round_id` columns independently.
- [ ] Write failing state-machine tests for planned, running, success, empty, failed, skipped, reused, and interrupted outcomes, including structured fallback evidence.
- [ ] Write failing five-state day-cell tests and role-consumption tests proving forecast excludes cross-sectional probes while raw T-curve reports role composition.
- [ ] Prewrite the deduplicated collection plan, update outcomes at the cache/fetch boundary, and finalize unfinished cells as interrupted.
- [ ] Add exact current-round lineage to new price-history writes; leave historical and unavailable lineage null.
- [ ] Run explicit production migrations after backup and prove old rows remain readable; document rollback for each migration section.
- [ ] Run all regression and state-hash gates.
- [ ] Commit as `feat: add collection outcome ledger and round lineage`, push, and wait for all CI cells.

### Task 3: Hybrid research basket

**Files:**
- Modify: `basket_collect.py`
- Modify: `collection_plan.py`
- Modify: `config.yaml`
- Modify: `test_basket_collect.py`
- Create: `test_research_cohort_v2.py`
- Create: `scripts/research_quota_simulation.py`
- Create: `docs/research-cohort-v2-2026-08-26.md`

- [ ] Write failing tests for two fixed anchors, four rolling probes, valid-only rotation, T=0 anchor collection, post-departure completion, collision deduplication, paused routes, and the default-off switch.
- [ ] Implement the six-slot versioned state without adding requests when `RESEARCH_COHORT_V2=false`.
- [ ] Derive probe progress from valid ledger cells; failed, empty, and skipped outcomes do not advance a slot.
- [ ] Add whole-system quota simulation output and hard-gate status fields; do not enable the switch.
- [ ] Prove legacy basket behavior is unchanged while disabled and run full regressions and state-hash gates.
- [ ] Commit as `feat: rebalance research basket into hybrid sampling`, push, and wait for all CI cells.

### Final Verification

- [ ] Confirm the three commits are separate and ordered.
- [ ] Confirm no real API calls and report quota/state hashes before and after.
- [ ] Confirm production timestamp and lineage migrations completed while the research cohort switch remains false.
- [ ] Report off-disk backup and quota hard-gate status honestly; only the user may enable the cohort.
- [ ] Determine PA Reload from the actual import chain touched by each commit.
