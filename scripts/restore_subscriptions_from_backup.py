"""Restore only data/subscriptions.json from one verified runtime backup (R9-3).

默认只读预览：把备份隔离恢复到临时目录并完整核验，打印备份身份、订阅成员与订阅文件 SHA-256，
随后删除临时目录；不写 backup_status.json，不碰生产目录。
回填须同时给出 --apply、--confirm RESTORE-SUBSCRIPTIONS 与 --expect-sha256(取预览打印的值)：
持有采集单飞锁，在订阅文件锁内确认目标仍不存在，再把备份中的原始字节原子发布并读回核对。
不启动采集，不写其他文件，不访问 PythonAnywhere。规程见 docs/subscriptions-single-file-restore.md。
退出码：0 成功；1 失败(生产文件未改动)；2 拒绝(生产文件未改动)；3 已写入但读回核对未通过(须立即人工核对)。
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collection_singleflight import (  # noqa: E402
    acquire_collection_singleflight,
    resolve_collection_lock_path,
)
from local_file_lock import FileLockTimeout, file_lock  # noqa: E402
from runtime_restore import RuntimeRestoreError, restore_runtime_backup  # noqa: E402
from subscription_preflight import (  # noqa: E402
    evaluate_subscription_preflight,
    shanghai_today,
)


CONFIRMATION_WORD = "RESTORE-SUBSCRIPTIONS"
SUBSCRIPTIONS_SOURCE_REL = "subscriptions.json"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_PUBLISHED_UNVERIFIED = 3
PUBLISH_REPLACE_RETRIES = 2
PUBLISH_REPLACE_RETRY_DELAY_SECONDS = 0.05
ISOLATED_CLEANUP_ATTEMPTS = 3
ISOLATED_CLEANUP_RETRY_DELAY_SECONDS = 0.2
MISSING_STATUS = "(missing)"


class SubscriptionRestoreRefused(RuntimeError):
    """回填前提不满足：不写任何生产文件，退出码 2。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class SubscriptionBackupInvalid(RuntimeError):
    """备份中没有唯一的订阅文件，或其内容不是 JSON 数组：退出码 1。"""


class SubscriptionPublishedUnverified(RuntimeError):
    """订阅文件已原子写入，但读回失败或 SHA-256 不符：退出码 3，须立即人工核对。"""


def subscriptions_target(project_root: str | Path = ROOT) -> Path:
    return Path(project_root).resolve() / "data" / "subscriptions.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _subscriptions_member(manifest: dict) -> str:
    matches = [
        item
        for item in manifest.get("files") or []
        if isinstance(item, dict) and item.get("source_rel") == SUBSCRIPTIONS_SOURCE_REL
    ]
    if (
        len(matches) != 1
        or not matches[0].get("present")
        or matches[0].get("entry_type") == "directory"
        or not str(matches[0].get("path") or "")
    ):
        raise SubscriptionBackupInvalid("备份 manifest 中没有唯一且存在的 subscriptions.json")
    return str(matches[0]["path"])


def _value(subscription: dict, key: str):
    """与 subscription_preflight._first_value 相同的取值顺序。"""
    value = subscription.get(key)
    if value not in (None, ""):
        return value
    for section_name in ("hard_constraints", "constraints", "basic", "preferences"):
        section = subscription.get(section_name)
        if isinstance(section, dict) and section.get(key) not in (None, ""):
            return section[key]
    return None


def _collected_by(subscription: dict) -> list[str]:
    """哪些采集入口会把这条订阅当作 active(规则照抄各入口，任一命中即计入 active)。"""

    entries = []
    if subscription.get("status", "active") == "active":  # main.load_file_subscriptions
        entries.append("main")
    status = str(subscription.get("status") or "active").strip().lower()
    if status in {"active", "enabled"}:  # basket_collect 读取研究订阅输入
        entries.append("basket_collect")
    return entries


def _describe_members(subscriptions: list, *, today) -> tuple[list[dict], list[dict]]:
    """逐条列出可核对字段；不输出邮箱、预算、令牌等其他字段。"""

    members = []
    needs_attention = []
    for index, item in enumerate(subscriptions):
        if not isinstance(item, dict):
            needs_attention.append({"index": index, "reason": "not_object"})
            continue
        identifier = str(item.get("id") or item.get("subscription_id") or "")
        if not identifier:
            needs_attention.append({"index": index, "reason": "missing_id"})
        collected_by = _collected_by(item)
        if collected_by and item.get("status") != "active":
            needs_attention.append({"index": index, "reason": "status_nonstandard"})
        try:
            preflight = evaluate_subscription_preflight(item, today=today)
            preflight_skip = (
                str(preflight.get("reason_code") or "skip") if preflight.get("skip") else ""
            )
        except Exception as exc:  # 只读预览：单条异常只标注，不中断核对
            preflight_skip = f"preflight_error:{type(exc).__name__}"
        members.append(
            {
                "index": index,
                "id": identifier,
                "status": str(item["status"]) if "status" in item else MISSING_STATUS,
                "collected_by": collected_by,
                "origin": str(_value(item, "origin") or ""),
                "destination": str(_value(item, "destination") or ""),
                "depart_date": str(
                    _value(item, "depart_date") or _value(item, "departure_date") or ""
                ),
                "return_date": str(_value(item, "return_date") or ""),
                "preflight_skip": preflight_skip,
            }
        )
    return members, needs_attention


def inspect_backup_subscriptions(
    archive_path: str | Path,
    *,
    checksum_path: str | Path | None = None,
    today=None,
) -> tuple[dict, bytes]:
    """隔离恢复并核验整份备份，取出订阅文件原始字节；隔离目录在返回前删除，删不掉时报出路径。"""

    directory = Path(tempfile.mkdtemp(prefix="flight-monitor-subscriptions-restore-"))
    try:
        restored = restore_runtime_backup(
            archive_path,
            checksum_path=checksum_path,
            destination=directory / "restored",
            status_path=None,
        )
        root = Path(restored["path"]).resolve()
        manifest = restored["manifest"]
        member = _subscriptions_member(manifest)
        source = (root / PurePosixPath(member)).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise SubscriptionBackupInvalid("订阅文件路径越过隔离恢复目录") from exc
        if not source.is_file():
            raise SubscriptionBackupInvalid("隔离恢复目录中缺少订阅文件")
        raw = source.read_bytes()
    finally:
        leftover = _remove_isolated_copy(directory)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubscriptionBackupInvalid(
            f"备份中的订阅文件不是有效 JSON ({type(exc).__name__})"
        ) from exc
    if not isinstance(payload, list):
        raise SubscriptionBackupInvalid("备份中的订阅文件顶层不是数组")
    members, needs_attention = _describe_members(payload, today=today or shanghai_today())
    statuses = Counter(item["status"] for item in members)
    active_members = [item for item in members if item["collected_by"]]
    summary = {
        "backup_id": manifest.get("backup_id"),
        "backup_created_at_utc": manifest.get("created_at_utc"),
        "archive_sha256": restored["archive_sha256"],
        "archive_member": member,
        "subscriptions_sha256": _sha256(raw),
        "subscriptions_bytes": len(raw),
        "subscription_count": len(payload),
        "active_count": len(active_members),
        "active_ids": [item["id"] for item in active_members if item["id"]],
        "status_counts": dict(sorted(statuses.items())),
        "subscriptions": members,
        "needs_attention": needs_attention,
        "isolated_copy_left": leftover,
    }
    return summary, raw


def _remove_isolated_copy(directory: Path) -> str | None:
    for attempt in range(ISOLATED_CLEANUP_ATTEMPTS):
        shutil.rmtree(directory, ignore_errors=True)
        if not directory.exists():
            return None
        if attempt + 1 < ISOLATED_CLEANUP_ATTEMPTS:
            time.sleep(ISOLATED_CLEANUP_RETRY_DELAY_SECONDS * (attempt + 1))
    print(
        f"[订阅恢复] 警告 隔离恢复副本未能删除(内含订阅与数据库副本)，请手动删除: {directory}",
        file=sys.stderr,
    )
    return str(directory)


def _read_back(target: Path) -> bytes:
    return target.read_bytes()


def _publish_bytes_atomic(target: Path, payload: bytes) -> None:
    temporary = target.with_name(f"{target.name}.{uuid4().hex}.restore.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(PUBLISH_REPLACE_RETRIES + 1):
            try:
                os.replace(temporary, target)
                break
            except PermissionError:
                if attempt >= PUBLISH_REPLACE_RETRIES:
                    raise
                time.sleep(PUBLISH_REPLACE_RETRY_DELAY_SECONDS * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _no_side_effects() -> dict:
    return {
        "backup_status_written": False,
        "collection_started": False,
        "real_api_calls": 0,
    }


def preview_restore(
    archive_path: str | Path,
    *,
    checksum_path: str | Path | None = None,
    project_root: str | Path = ROOT,
    today=None,
) -> dict:
    target = subscriptions_target(project_root)
    summary, _raw = inspect_backup_subscriptions(
        archive_path,
        checksum_path=checksum_path,
        today=today,
    )
    return {
        "operation": "preview",
        "passed": True,
        **summary,
        "target": str(target),
        "target_exists": _target_present(target),
        "production_state_changed": False,
        **_no_side_effects(),
        "apply_arguments": (
            f"--apply --confirm {CONFIRMATION_WORD} "
            f"--expect-sha256 {summary['subscriptions_sha256']}"
        ),
    }


def apply_restore(
    archive_path: str | Path,
    *,
    confirmation: str,
    expected_sha256: str,
    checksum_path: str | Path | None = None,
    project_root: str | Path = ROOT,
    lock_path: str | Path | None = None,
    today=None,
) -> dict:
    target = subscriptions_target(project_root)
    if confirmation != CONFIRMATION_WORD:
        raise SubscriptionRestoreRefused(
            "confirmation_mismatch",
            f"确认串必须是 {CONFIRMATION_WORD}；本次未回填",
        )
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise SubscriptionRestoreRefused(
            "expected_sha256_invalid",
            "--expect-sha256 必须是预览输出的 64 位十六进制 subscriptions_sha256；本次未回填",
        )
    if _target_present(target):
        raise SubscriptionRestoreRefused(
            "target_exists",
            f"订阅文件已存在，拒绝覆盖: {target}；请先核对现有文件",
        )
    effective_lock_path = (
        Path(lock_path)
        if lock_path is not None
        else resolve_collection_lock_path(base_dir=Path(project_root).resolve())
    )
    gate = acquire_collection_singleflight(
        f"restore_subscriptions_{uuid4().hex[:12]}",
        lock_path=effective_lock_path,
        heartbeat_interval_seconds=0,
    )
    if not gate.acquired:
        raise SubscriptionRestoreRefused(
            "collection_busy",
            "采集单飞锁被占用，未回填；确认计划任务已停用、当前采集结束后再执行",
        )
    try:
        summary, raw = inspect_backup_subscriptions(
            archive_path,
            checksum_path=checksum_path,
            today=today,
        )
        if summary["subscriptions_sha256"] != expected:
            raise SubscriptionRestoreRefused(
                "sha256_mismatch",
                "备份中的订阅文件与 --expect-sha256 不一致，未回填；请重新预览核对",
            )
        try:
            with file_lock(target):
                if _target_present(target):
                    raise SubscriptionRestoreRefused(
                        "target_exists",
                        f"订阅文件已被其他入口重建，未覆盖: {target}；请先核对该文件再决定",
                    )
                _publish_bytes_atomic(target, raw)
                try:
                    published_sha256 = _sha256(_read_back(target))
                except OSError as exc:
                    raise SubscriptionPublishedUnverified(
                        f"订阅文件已写入但读回失败({type(exc).__name__})，请立即人工核对: {target}"
                    ) from exc
        except FileLockTimeout as exc:
            raise SubscriptionRestoreRefused(
                "subscriptions_lock_busy",
                "订阅文件锁被占用，未回填；确认没有其他写入方后再执行",
            ) from exc
    finally:
        gate.release()
    if published_sha256 != expected:
        raise SubscriptionPublishedUnverified(
            f"订阅文件已写入但读回的 SHA-256 与预期不一致，请立即人工核对: {target}"
        )
    return {
        "operation": "apply",
        "passed": True,
        **summary,
        "target": str(target),
        "published": True,
        "published_sha256": published_sha256,
        "production_state_changed": True,
        **_no_side_effects(),
        "next_step": "解除维护前逐条核对 collected_by 非空的订阅(含 preflight_skip 非空者与 needs_attention)，再恢复计划任务",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "从经校验的运行备份只回填 data/subscriptions.json。默认只读预览："
            "不写 backup_status.json，不改生产目录，不启动采集，不调用任何 API。"
        ),
        epilog=(
            f"回填须同时提供 --apply、--confirm {CONFIRMATION_WORD} 与 --expect-sha256"
            "(取预览输出的 subscriptions_sha256)；目标文件已存在时一律拒绝。"
        ),
    )
    parser.add_argument("--archive", required=True, help="运行备份归档 .tar.gz 路径")
    parser.add_argument("--checksum", help="可选SHA256旁路文件，默认<archive>.sha256")
    parser.add_argument(
        "--project-root",
        default=str(ROOT),
        help="项目根目录，回填目标为其下 data/subscriptions.json",
    )
    parser.add_argument("--apply", action="store_true", help="执行回填(默认只读预览)")
    parser.add_argument("--confirm", default="", help=f"回填确认串，必须为 {CONFIRMATION_WORD}")
    parser.add_argument(
        "--expect-sha256",
        default="",
        help="预览输出的 subscriptions_sha256，把回填绑定到已审阅的那一版",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        if args.apply:
            result = apply_restore(
                args.archive,
                confirmation=args.confirm,
                expected_sha256=args.expect_sha256,
                checksum_path=args.checksum,
                project_root=args.project_root,
            )
        else:
            if args.confirm or args.expect_sha256:
                raise SubscriptionRestoreRefused(
                    "apply_flag_missing",
                    "给出 --confirm 或 --expect-sha256 时必须同时给出 --apply；本次未回填",
                )
            result = preview_restore(
                args.archive,
                checksum_path=args.checksum,
                project_root=args.project_root,
            )
    except SubscriptionRestoreRefused as exc:
        print(f"[订阅恢复] 拒绝 reason={exc.reason} {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except SubscriptionPublishedUnverified as exc:
        print(f"[订阅恢复] 已写入未核验 {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_PUBLISHED_UNVERIFIED
    except (
        RuntimeRestoreError,
        SubscriptionBackupInvalid,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"[订阅恢复] 失败 {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
