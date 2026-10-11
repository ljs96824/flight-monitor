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

## Runner Images

- 2026-10-11 依据 actions/runner-images#14748：`ubuntu-latest` 自 2026-10-19 起分批改指
  Ubuntu 26.04，计划 2026-11-19 完成。
- `tests` 矩阵的 Ubuntu 项与 `ui-smoke` 固定为 `ubuntu-24.04`，避免没有仓库提交时运行环境漂移；
  `windows-latest` 维持可移动标签。
- 采用 Ubuntu 26.04 须单独评审，并以 PR 双平台 CI 验证，不随标签自动切换。
- 矩阵值即 check 名：Ubuntu 测试的 required check 为 `tests (ubuntu-24.04)`，分支保护须同步替换
  原 `tests (ubuntu-latest)`。
