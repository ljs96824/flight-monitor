# format_comparison_message F821 Implementation Plan

> **For Codex:** Follow the repository TDD and verification gates. Do not call a real flight API.

**Goal:** Remove every undefined-name path from `format_comparison_message`, preserve the current notification pipelines, and add an exact-debt Ruff F821 gate.

**Approach:** Treat the legacy comparison details as unavailable because its historical summary helper depended on removed subjective purchase advice. A private detail builder raises `ComparisonMessageUnavailable`; the public compatibility entry catches it, emits a structured log, and renders only evidence already present in its inputs. Existing F821 findings outside this function remain an exact `(path, scope, symbol)` debt set.

**Stack:** Python 3.13, unittest/pytest, Ruff, GitHub Actions.

## Tasks

1. Record the full Ruff F821 baseline and map findings to AST scope triples.
2. Add RED tests for the current NameError, four notification-pipeline reachability probes, and honest fallback behavior.
3. Implement `ComparisonMessageUnavailable`, the private detail circuit breaker, and a conservative public fallback without inferred comparison claims.
4. Add `scripts/check_f821.py` with the exact remaining debt set and contract tests for zero target-function findings.
5. Add Ruff only to `requirements-dev.in`, regenerate `requirements-dev.txt` in a clean Python 3.13 environment, and add separate CI F821/import gates before behavior tests.
6. Document symbol provenance, helper-semantics comparison, reachability evidence, and the retained debt set.
7. Run targeted tests, both full collectors, frozen email/PushPlus/fixture/snapshot regressions, and compare production-state hashes.
8. Create one independent commit; do not push without explicit authorization.
