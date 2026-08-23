# Snapshot Artifacts Out-of-Repository Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one-off snapshot outputs local and ignored while preserving deterministic snapshot tooling and documenting historical exposure without rewriting Git history.

**Architecture:** Snapshot producers write to `data/snapshots/` by default, while explicit `--output` paths remain supported. Root-level one-off outputs are ignored and removed from Git tracking without deleting local copies. The privacy audit records only field categories and counts from historical blobs, never historical values.

**Tech Stack:** Python 3.13, `unittest`, Git, Markdown.

---

### Task 1: Lock snapshot artifact hygiene with tests

**Files:**
- Create: `test_snapshot_artifact_hygiene.py`
- Modify: `scripts/snapshot_run.py`

- [ ] Add a test for the default `data/snapshots/snapshot.json` path.
- [ ] Add Git-backed tests proving root snapshot outputs are ignored and untracked.
- [ ] Run the focused test and confirm it fails before implementation.
- [ ] Add the minimal output-path resolver and rerun the focused test.

### Task 2: Remove one-off artifacts from the repository

**Files:**
- Modify: `.gitignore`
- Remove from tracking only: root `before`/`after` JSON, snapshot checks, generated HTML/log/diff outputs.

- [ ] Add root-anchored ignore patterns for one-off snapshot products.
- [ ] Use `git rm --cached` so local files remain available.
- [ ] Confirm no production or test consumer references the removed outputs.

### Task 3: Record historical exposure and verify

**Files:**
- Modify: `docs/privacy-exposure-audit-2026-08-21.md`

- [ ] Record the historical blob/field-path counts and exposed field categories.
- [ ] Record the direct-identifier scan result and the no-rewrite decision with costs.
- [ ] Run the snapshot using its default path and verify Git remains clean after commit.
- [ ] Run pytest and unittest, confirm the API usage ledger hash is unchanged, and create one independent commit.
