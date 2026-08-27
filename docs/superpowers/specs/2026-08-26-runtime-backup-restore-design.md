# Runtime Backup and Restore Design

## Objective

Build a versioned, offline backup system that captures the complete runtime state needed to recover the flight monitor after disk loss, validates every captured artifact, restores only into an isolated directory by default, and proves replay equivalence for the T-curve and forecast reports.

The system must never call a flight API. A backup is considered valid only after a restore and replay rehearsal succeeds.

## Boundaries

The implementation adds three focused modules:

- `runtime_backup.py`: versioned inventory, strict classification, two-phase capture, archive publication, and sanitized summaries.
- `runtime_restore.py`: archive and member validation, safe extraction, restored-state validation, replay comparison, and guarded production restore.
- `scripts/runtime_backup.py`: the `create`, `verify`, `restore`, and `rehearse` command-line interface.

Existing `readonly_snapshot.create_readonly_snapshot()` remains the source of truth for SQLite online backup and the compatible `core_snapshot/` layout. Existing `collection_singleflight.acquire_collection_singleflight()` is the outermost non-blocking lock. Existing `local_file_lock.file_lock()` supplies the JSON locks.

## Versioned Inventory

`RUNTIME_BACKUP_SPEC` originally shipped as `runtime_backup_v1`. The
2026-08-27 runtime-configuration split raised it to `runtime_backup_v2`, whose
required core additionally includes `state/runtime_config.yaml`; see
`docs/runtime-config-separation-2026-08-27.md`.

| Tier | Runtime paths | Capture rule |
| --- | --- | --- |
| required core | `prices.db`, `observations.sqlite3`, `subscriptions.json`, `api_usage.json` | Missing is fatal |
| business state | `feedback.json`, `price_calendar/`, `pushed_plans/`, `basket_state.json`, `basket_sentinel.json`, `signals_history.jsonl` | If present, capture in full |
| evidence | `payloads/`, recent files under `logs/rounds/` | Controlled by CLI options |
| diagnostics | `monitor.log`, `analysis_log.jsonl`, `notifications_log.txt` | Included only with `--include-diagnostics` |
| excluded | secrets, locks, caches, snapshots, existing backups, temporary files, development captures, legacy detail stores, raw/debug responses, and transient process logs | Never captured |

The strict scanner classifies every path below `data/` by exact path or a reviewed prefix/pattern. New unclassified paths fail capture before the single-flight lock is acquired. This makes future runtime state additions visible instead of silently omitting them.

Current diagnostic and development artifacts not listed as required state are explicitly classified as excluded, with a reason in the spec. They are not hidden behind a broad catch-all.

## Capture Protocol

Phase A is the short consistency window:

1. Validate that `--output-dir` is absolute and outside both project and data roots.
2. Strictly classify `data/` and fail on unknown paths.
3. Acquire collection single-flight non-blockingly. Busy returns status `busy`, exit code 2, and creates neither archive nor final manifest.
4. Create staging with `TemporaryDirectory(dir=output_root)`.
5. Acquire JSON locks in the fixed order `api_usage -> subscriptions -> feedback` using `ExitStack`.
6. Run `create_readonly_snapshot()` into `staging/core_snapshot`, including frozen `permission_quality_cells`.
7. Strictly parse and copy JSON state, copy other business state, and freeze/copy configured evidence.
8. Generate the private top-level staging manifest.
9. Release JSON locks and collection single-flight.

Phase B runs without locks:

1. Build a tar.gz from staging.
2. Flush and fsync the archive.
3. Set mode 0600 and atomically publish with `os.replace`.
4. Write the archive SHA-256 to an atomic sidecar file. The archive never contains its own archive hash.

Directories are created with mode 0700 where supported.

## Manifest Contract

The private `manifest.json` contains:

- `manifest_version`, `backup_id`, `created_at_utc`, `git_commit`, `python_version`
- `runtime_backup_spec_version`
- `capture_consistency` booleans for single-flight, SQLite online backup, and locked JSON reads
- one `files[]` item per present or configured-absent artifact
- for each present file: archive path, kind, required flag, byte count, SHA-256, JSON parse status or SQLite integrity result, SQLite user version, and table row counts

Public command output is deliberately smaller: backup id, archive hash, file count, total bytes, SQLite integrity summary, JSON validation summary, replay hashes, production-state-changed boolean, and real API calls. It never prints subscription names, payload identifiers, routes, dates, emails, or table values.

## Restore Protocol

Restore verifies the archive SHA sidecar before opening the archive. It rejects absolute paths, parent traversal, links, devices, excessive members, excessive expanded bytes, and extraction outside the destination. It uses Python's data extraction filter where available and performs its own path and member-type checks.

The default destination must not already exist. After extraction, restore verifies every manifest SHA, parses each JSON file, and runs SQLite `integrity_check`. It never contacts an API, reloads a service, or modifies production state.

Production restore is an advanced guarded path requiring both `--force-production` and `--confirm-production-restore RESTORE`. It acquires collection single-flight, creates and verifies a pre-restore backup outside the project, verifies the candidate in staging, moves current runtime paths to a rollback directory, switches the restored paths into place, and rolls back on any failure. This path is implemented and tested with isolated fixtures but is not used in the real rehearsal.

## Replay Contract

The rehearsal generates `tcurve_source.txt` and `forecast_source.txt` from the captured `core_snapshot/`, archives the staging tree, restores to a new directory, generates the reports again from `restored/core_snapshot/`, and requires byte-identical SHA-256 values. Both report runs use the same frozen `snapshot_manifest.json`, including `permission_quality_cells`.

The route is an explicit rehearsal input and is not written into the sanitized public summary.

## Failure Semantics

- Existing collection: return `busy`, exit 2, no archive, no final manifest.
- Missing required state, unknown data path, invalid JSON, or bad SQLite integrity: fail before publication.
- Archive or member hash mismatch: reject restore before use.
- Optional missing state: record `present=false` in the private manifest and continue.
- Any exception before atomic publication removes staging and leaves previous archives unchanged.

## Security Notes

The archive contains private operational data such as email addresses, budgets, routes, and rendered payloads. It is not encrypted and must not be uploaded to a public or shared cloud directory. For off-device storage, use an operating-system encrypted volume or established encryption such as age or 7-Zip AES; this project does not implement cryptography.

## Acceptance

Tests use temporary data roots and never touch production databases. Final validation records hashes for production core state before and after, runs both test collectors, performs one real offline create/restore/replay rehearsal outside the project tree, confirms zero real API calls, and commits the implementation independently.
