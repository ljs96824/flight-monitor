# GitHub Actions Version Policy

- Status: Accepted
- Date: 2026-08-29
- Scope: `.github/workflows/tests.yml`

## Context

The repository uses official GitHub Actions for checkout, Python and Node setup,
and failure-artifact upload. The project favors receiving official security and
maintenance fixes within a reviewed major version while keeping behavior guarded
by offline tests, frozen render baselines, and required checks on Ubuntu, Windows,
and Chromium.

## Decision

官方大版本标签可移动；同一个仓库提交在不同日期运行时，Action底层提交可能发生变化。本仓选择自动接收同主版本安全补丁，不宣称Action执行代码完全可复现。

The workflow uses the official movable stable major tags verified on 2026-08-29:

| Action and verified stable release | Workflow reference |
| --- | --- |
| actions/checkout v7.0.1 | actions/checkout@v7 |
| actions/setup-python v7.0.0 | actions/setup-python@v7 |
| actions/setup-node v7.0.0 | actions/setup-node@v7 |
| actions/upload-artifact v7.0.1 | actions/upload-artifact@v7 |

A newly published major version is not adopted automatically. Its breaking
changes, runner requirements, and compatibility with this repository must be
reviewed first.

## Security Boundary

- Workflow-level `GITHUB_TOKEN` permission is limited to `contents: read`.
- Jobs must not widen token permissions.
- Every checkout step uses `persist-credentials: false`.
- Artifact upload does not justify granting repository write permissions.

## Consequences

- Official fixes within v7 can change the underlying Action commit without a
  repository commit.
- Required checks are the cross-platform acceptance boundary for those changes.
- Dependency lock files and the pinned Playwright package remain separate
  reproducibility controls and are not changed by this policy.
