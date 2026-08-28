# Relative Time Timezone Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve timestamp offsets when rendering subscription-list relative times so the label is independent of the Web server's local timezone.

**Architecture:** Reuse `project_time.SHANGHAI_TZ` as the sole interpretation for legacy naive display timestamps, normalize parsed values and the injectable clock to UTC-aware datetimes, then retain the existing Chinese label thresholds and invalid-input behavior. The change stays inside `web_form.py`; a focused unittest module locks parsing, timezone equivalence, midnight, future-clock, and threshold behavior.

**Tech Stack:** Python 3.13 standard-library `datetime`/`zoneinfo`, unittest, existing project time utilities.

---

### Task 1: Lock the timezone regression

**Files:**
- Create: `test_web_relative_time.py`

- [ ] **Step 1: Write the failing tests**

Cover `Z`, `+08:00`, `+00:00`, legacy naive Shanghai timestamps, timezone-independent `now`, local-midnight crossing, slight future timestamps, 59/60-minute and 23/24-hour boundaries, and existing empty/invalid behavior.

- [ ] **Step 2: Verify RED**

Run: `python -X utf8 -m unittest test_web_relative_time -v`

Expected: FAIL because the current implementation discards parsed offsets and does not accept the injectable `now` keyword.

### Task 2: Normalize display timestamps through the project timezone

**Files:**
- Modify: `web_form.py`
- Test: `test_web_relative_time.py`

- [ ] **Step 1: Add `_parse_display_time()`**

Parse ISO text, interpret naive values with `project_time.SHANGHAI_TZ`, and return an aware UTC datetime.

- [ ] **Step 2: Make `_relative_time_label(..., now=None)` timezone-aware**

Use aware UTC `datetime.now(timezone.utc)` by default; treat an injected naive clock as UTC; preserve all existing labels and invalid-input fallbacks.

- [ ] **Step 3: Verify GREEN**

Run: `python -X utf8 -m unittest test_web_relative_time -v`

Expected: all matrix cases pass.

### Task 3: Verify and deliver

**Files:**
- Modify only if evidence requires: none

- [ ] **Step 1: Run affected and full collectors**

Run the focused module, Anaconda pytest, and production-Python unittest discovery without live API access.

- [ ] **Step 2: Verify frozen outputs and UI**

Run frozen email/PushPlus contracts and `scripts/ui_smoke.py` on an isolated temporary port.

- [ ] **Step 3: Recheck state hashes**

Confirm `prices.db`, `observations.sqlite3`, `subscriptions.json`, and `api_usage.json` match their pre-change SHA-256 values.

- [ ] **Step 4: Commit and push**

Create one independent fix commit, push through the protected-branch workflow, and wait for Ubuntu/Windows collectors plus blocking UI smoke.
