# Runtime Backup and Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce an atomic, verifiable runtime backup, safely restore it to isolation, and prove T-curve and forecast replay equality without touching production state or calling an API.

**Architecture:** `runtime_backup.py` owns the versioned inventory and two-phase capture; `runtime_restore.py` owns hostile-archive validation, restore verification, replay, and guarded production replacement. `scripts/runtime_backup.py` exposes a small subcommand CLI, while existing readonly snapshot and report modules remain unchanged and authoritative.

**Tech Stack:** Python 3.13 standard library (`tarfile`, `sqlite3`, `hashlib`, `tempfile`, `pathlib`, `contextlib`), existing local lock and readonly snapshot modules, `unittest`, `pytest`.

---

### Task 1: Freeze the inventory and capture contract

**Files:**
- Create: `runtime_backup.py`
- Create: `test_runtime_backup.py`

- [ ] Write tests for missing required files, optional absent manifest entries, strict unknown-file rejection, output-root rejection, and busy exit semantics.
- [ ] Run `python -X utf8 -m pytest -q -p no:cacheprovider test_runtime_backup.py` and verify failures are caused by the missing module/API.
- [ ] Implement `RUNTIME_BACKUP_SPEC`, path classification, output validation, and a non-blocking capture result.
- [ ] Add tests proving JSON locks are acquired in `api_usage -> subscriptions -> feedback` order and released before archive compression.
- [ ] Implement Phase A with `ExitStack`, `create_readonly_snapshot()`, frozen permission quality metadata, strict JSON reads, evidence selection, and a private staging manifest.

### Task 2: Atomically publish a validated archive

**Files:**
- Modify: `runtime_backup.py`
- Modify: `test_runtime_backup.py`

- [ ] Write failing tests for archive structure, manifest fields, 0600/0700 permissions, atomic publication, and sidecar SHA.
- [ ] Implement deterministic member naming, tar.gz creation outside the lock, fsync, chmod, and `os.replace` publication.
- [ ] Verify a simulated pre-replace failure leaves no final archive or manifest.

### Task 3: Restore hostile archives safely

**Files:**
- Create: `runtime_restore.py`
- Create: `test_runtime_restore.py`

- [ ] Write failing tests for bad archive SHA, member SHA mismatch, corrupt SQLite, corrupt JSON, path traversal, symlink/hardlink/device members, size/member limits, and existing destinations.
- [ ] Implement sidecar verification, safe extraction, manifest verification, strict JSON parsing, and SQLite integrity checks.
- [ ] Write failing tests for missing production confirmation and rollback after a simulated switch failure.
- [ ] Implement the guarded production restore path with pre-restore backup, staging validation, rollback directory, and immediate rollback.

### Task 4: Add report replay and CLI

**Files:**
- Create: `scripts/runtime_backup.py`
- Modify: `runtime_backup.py`
- Modify: `runtime_restore.py`
- Modify: `test_runtime_backup.py`
- Modify: `test_runtime_restore.py`

- [ ] Write failing tests for `create`, `verify`, `restore`, and `rehearse` exit codes and sanitized output.
- [ ] Write a failing replay test that captures reports, restores the archive, reruns both reports, and compares SHA-256.
- [ ] Implement report generation from the frozen core snapshot and persist source report hashes in the private manifest before archive publication.
- [ ] Implement the CLI and ensure busy returns 2 while all validation errors return nonzero without leaking private paths or values.

### Task 5: Document operations and run end-to-end verification

**Files:**
- Create: `docs/runtime-backup-and-restore.md`
- Modify: `test_docs_accuracy.py`

- [ ] Write documentation tests for the operation manual, explicit absolute output root, encryption warning, production confirmation phrase, and weekly/pre-change rehearsal discipline.
- [ ] Write the manual with create, verify, isolated restore, rehearsal, and advanced production restore commands.
- [ ] Run focused tests, then both full collectors with pytest cache disabled.
- [ ] Hash production core state and API usage, perform one backup to an external local directory, restore to a new temporary directory, verify, replay both reports, and compare post-run hashes.
- [ ] Stage only task files, create one independent commit, and verify a clean task diff.
