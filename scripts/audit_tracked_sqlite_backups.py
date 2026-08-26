#!/usr/bin/env python3
"""Strictly read-only audit of SQLite artifacts tracked by Git.

The scanner never emits row values. Confirmed credential material blocks public
report generation so private evidence cannot accidentally enter version control.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRACKED_SQLITE_PATTERNS = (
    "*.sqlite3",
    "*.db",
    "*.bak",
    "*-wal",
    "*-shm",
    "*-journal",
)
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

CATEGORY_LABELS = {
    "secret_credentials": "秘密凭据",
    "direct_personal_information": "直接个人信息",
    "personal_itinerary_metadata": "个人行程元数据",
    "anonymous_market_observations": "匿名市场观测",
    "high_entropy_identifier": "高熵标识符",
}
CATEGORY_RISK = {
    "secret_credentials": "严重",
    "direct_personal_information": "高",
    "personal_itinerary_metadata": "高",
    "anonymous_market_observations": "低",
    "high_entropy_identifier": "中",
}

SECRET_COLUMN_TOKENS = {
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
}
PII_COLUMN_TOKENS = {
    "email",
    "phone",
    "mobile",
    "telephone",
    "name",
    "address",
    "contact",
}
ITINERARY_COLUMN_TOKENS = {
    "subscription",
    "constraint",
    "fingerprint",
    "budget",
    "passenger",
    "origin",
    "destination",
    "depart_date",
    "departure_date",
    "return_date",
    "meeting",
    "travel_scenario",
    "companion",
}
MARKET_COLUMN_TOKENS = {
    "price",
    "fare",
    "source",
    "flight",
    "airline",
    "cabin",
    "stops",
    "duration",
}

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AIRPORT_RE = re.compile(r"^[A-Z]{3}$")
SECRET_VALUE_RES = (
    re.compile(r"(?i)\b(?:sk|pk|rk)[-_](?:live|prod|test)?[-_A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
)
CONSTRAINT_MARKERS = (
    '"direct_only"',
    '"transfer_policy"',
    '"lcc_policy"',
    '"cabin_allocation"',
    '"need_baggage"',
)


class ReadOnlyContractViolation(RuntimeError):
    """Raised if an audited file or its SQLite sidecars change."""


class SecretCredentialDetected(RuntimeError):
    """Raised before a public report can be rendered for credential material."""


def _run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def discover_tracked_sqlite_artifacts(repo_root: str | Path = ROOT) -> list[str]:
    """List Git-tracked SQLite-like artifacts; ignored working files are irrelevant."""
    root = Path(repo_root).resolve()
    output = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", *TRACKED_SQLITE_PATTERNS],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return sorted({item for item in output.split("\0") if item})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_state(path: Path) -> dict[str, tuple[int, int, str]]:
    state = {}
    for suffix in SIDECAR_SUFFIXES:
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            stat = sidecar.stat()
            state[sidecar.name] = (stat.st_size, stat.st_mtime_ns, _sha256(sidecar))
    return state


def artifact_identity(repo_root: str | Path, relative_path: str) -> dict:
    root = Path(repo_root).resolve()
    path = root / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"tracked artifact is missing: {relative_path}")

    staged = _run_git(root, "ls-files", "-s", "--", relative_path).strip()
    if not staged:
        raise ValueError(f"artifact is not tracked by Git: {relative_path}")
    git_blob = staged.split()[1]
    history = [
        line.strip()
        for line in _run_git(
            root, "log", "--follow", "--format=%H", "--", relative_path
        ).splitlines()
        if line.strip()
    ]
    introduced = [
        line.strip()
        for line in _run_git(
            root,
            "log",
            "--diff-filter=A",
            "--follow",
            "--format=%H",
            "--",
            relative_path,
        ).splitlines()
        if line.strip()
    ]
    return {
        "path": relative_path,
        "git_blob": git_blob,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        "first_commit": introduced[-1] if introduced else (history[-1] if history else "unknown"),
        "last_commit": history[0] if history else "unknown",
    }


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _column_tokens(name: str) -> set[str]:
    lowered = str(name or "").strip().lower()
    parts = set(re.findall(r"[a-z0-9]+", lowered))
    parts.add(lowered)
    return parts


def _matches_column_rule(name: str, rules: set[str]) -> bool:
    lowered = str(name or "").strip().lower()
    tokens = _column_tokens(lowered)
    return any(
        rule in tokens
        or lowered == rule
        or lowered.startswith(rule + "_")
        or lowered.endswith("_" + rule)
        for rule in rules
    )


def _is_high_entropy(text: str) -> bool:
    if len(text) < 24 or len(text) > 4096:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", text):
        return True
    if not re.fullmatch(r"[A-Za-z0-9_./+=:-]+", text):
        return False
    counts = defaultdict(int)
    for char in text:
        counts[char] += 1
    entropy = -sum(
        (count / len(text)) * math.log2(count / len(text)) for count in counts.values()
    )
    return entropy >= 4.0


def _schema_objects(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    return [
        {
            "type": row["type"],
            "name": row["name"],
            "table_name": row["tbl_name"],
            "is_virtual": bool(
                row["type"] == "table"
                and str(row["sql"] or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE")
            ),
            "check_constraint_count": len(
                re.findall(r"(?i)\bCHECK\s*\(", str(row["sql"] or ""))
            ),
        }
        for row in rows
    ]


def _table_structure(connection: sqlite3.Connection, schema_objects: list[dict]) -> list[dict]:
    tables = []
    for obj in schema_objects:
        if obj["type"] != "table":
            continue
        name = obj["name"]
        columns = []
        for row in connection.execute(f"PRAGMA table_xinfo({_quote_identifier(name)})"):
            columns.append(
                {
                    "name": row["name"],
                    "type": str(row["type"] or ""),
                    "not_null": bool(row["notnull"]),
                    "has_default": row["dflt_value"] is not None,
                    "primary_key_position": int(row["pk"]),
                    "hidden": int(row["hidden"]),
                }
            )
        indexes = [
            {
                "name": row["name"],
                "unique": bool(row["unique"]),
                "origin": row["origin"],
                "partial": bool(row["partial"]),
            }
            for row in connection.execute(f"PRAGMA index_list({_quote_identifier(name)})")
        ]
        foreign_keys = list(
            connection.execute(f"PRAGMA foreign_key_list({_quote_identifier(name)})")
        )
        row_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(name)}"
            ).fetchone()[0]
        )
        tables.append(
            {
                "name": name,
                "row_count": row_count,
                "columns": columns,
                "indexes": indexes,
                "foreign_key_count": len(foreign_keys),
                "check_constraint_count": obj["check_constraint_count"],
                "is_virtual": obj["is_virtual"],
            }
        )
    return tables


def _empty_sensitive_state() -> dict:
    return {
        key: {"rows": set(), "columns": set()}
        for key in CATEGORY_LABELS
    }


def _scan_sensitive_content(connection: sqlite3.Connection, tables: list[dict]) -> dict:
    findings = _empty_sensitive_state()
    patterns = {
        "email_style": set(),
        "phone_style": set(),
        "high_entropy_string": set(),
        "route_or_date_record": set(),
        "constraint_structure": set(),
        "secret_credential_pattern": set(),
    }

    for table in tables:
        name = table["name"]
        column_names = [column["name"] for column in table["columns"] if not column["hidden"]]
        if not column_names:
            continue
        selected = ", ".join(_quote_identifier(column) for column in column_names)
        rows = connection.execute(f"SELECT {selected} FROM {_quote_identifier(name)}")
        for row_number, row in enumerate(rows, start=1):
            row_key = (name, row_number)
            for column_name, raw_value in zip(column_names, row):
                if raw_value is None or raw_value == "":
                    continue
                field_key = f"{name}.{column_name}"
                text = str(raw_value)

                if _matches_column_rule(column_name, SECRET_COLUMN_TOKENS):
                    findings["secret_credentials"]["rows"].add(row_key)
                    findings["secret_credentials"]["columns"].add(field_key)
                if _matches_column_rule(column_name, PII_COLUMN_TOKENS):
                    findings["direct_personal_information"]["rows"].add(row_key)
                    findings["direct_personal_information"]["columns"].add(field_key)
                if _matches_column_rule(column_name, ITINERARY_COLUMN_TOKENS):
                    findings["personal_itinerary_metadata"]["rows"].add(row_key)
                    findings["personal_itinerary_metadata"]["columns"].add(field_key)
                if _matches_column_rule(column_name, MARKET_COLUMN_TOKENS):
                    findings["anonymous_market_observations"]["rows"].add(row_key)
                    findings["anonymous_market_observations"]["columns"].add(field_key)

                if EMAIL_RE.search(text):
                    patterns["email_style"].add(row_key)
                    findings["direct_personal_information"]["rows"].add(row_key)
                    findings["direct_personal_information"]["columns"].add(field_key)
                if PHONE_RE.search(text):
                    patterns["phone_style"].add(row_key)
                    findings["direct_personal_information"]["rows"].add(row_key)
                    findings["direct_personal_information"]["columns"].add(field_key)
                if DATE_RE.fullmatch(text) or AIRPORT_RE.fullmatch(text):
                    patterns["route_or_date_record"].add(row_key)
                    findings["personal_itinerary_metadata"]["rows"].add(row_key)
                    findings["personal_itinerary_metadata"]["columns"].add(field_key)
                if any(marker in text for marker in CONSTRAINT_MARKERS):
                    patterns["constraint_structure"].add(row_key)
                    findings["personal_itinerary_metadata"]["rows"].add(row_key)
                    findings["personal_itinerary_metadata"]["columns"].add(field_key)
                if _is_high_entropy(text):
                    patterns["high_entropy_string"].add(row_key)
                    findings["high_entropy_identifier"]["rows"].add(row_key)
                    findings["high_entropy_identifier"]["columns"].add(field_key)
                if any(pattern.search(text) for pattern in SECRET_VALUE_RES):
                    patterns["secret_credential_pattern"].add(row_key)
                    findings["secret_credentials"]["rows"].add(row_key)
                    findings["secret_credentials"]["columns"].add(field_key)

    public_findings = {
        key: {
            "present": bool(value["rows"]),
            "matched_rows": len(value["rows"]),
            "matched_columns": len(value["columns"]),
            "risk": CATEGORY_RISK[key],
        }
        for key, value in findings.items()
    }
    public_patterns = {
        key: {"present": bool(rows), "matched_rows": len(rows)}
        for key, rows in patterns.items()
    }
    return {"categories": public_findings, "patterns": public_patterns}


def _classify_source(sensitivity: dict) -> tuple[str, str]:
    categories = sensitivity["categories"]
    if categories["secret_credentials"]["present"]:
        return "秘密凭据", "检测到秘密字段或强凭据模式，禁止公开报告"
    if categories["direct_personal_information"]["present"]:
        return "直接个人信息", "检测到直接个人信息字段或内容模式"
    if categories["personal_itinerary_metadata"]["present"]:
        return (
            "个人行程元数据",
            "存在路线、日期、订阅或约束类记录；无直接身份字段不足以证明其为纯匿名数据",
        )
    if categories["anonymous_market_observations"]["present"]:
        return "匿名市场观测", "仅检测到航班、来源或价格类市场观测结构"
    return "unknown", "结构与内容模式不足以可靠判定来源"


def audit_sqlite_file(path: str | Path) -> dict:
    """Audit one SQLite file and prove SHA, mtime and sidecars remain unchanged."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite artifact does not exist: {resolved}")
    before_hash = _sha256(resolved)
    before_mtime = resolved.stat().st_mtime_ns
    before_sidecars = _sidecar_state(resolved)
    uri = f"{resolved.as_uri()}?mode=ro&immutable=1"

    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise ReadOnlyContractViolation("PRAGMA query_only could not be enabled")
        integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        integrity = "ok" if integrity_rows == ["ok"] else "failed"
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema_objects = _schema_objects(connection)
        tables = _table_structure(connection, schema_objects)
        sensitivity = _scan_sensitive_content(connection, tables)

    after_hash = _sha256(resolved)
    after_mtime = resolved.stat().st_mtime_ns
    after_sidecars = _sidecar_state(resolved)
    if after_hash != before_hash or after_mtime != before_mtime or after_sidecars != before_sidecars:
        raise ReadOnlyContractViolation(
            "audited SQLite file, mtime, or sidecar state changed"
        )

    source_classification, classification_basis = _classify_source(sensitivity)
    return {
        "sha256": before_hash,
        "bytes": resolved.stat().st_size,
        "mtime_ns": before_mtime,
        "integrity_check": integrity,
        "user_version": user_version,
        "query_only": True,
        "immutable": True,
        "schema_objects": schema_objects,
        "tables": tables,
        "views": sorted(obj["name"] for obj in schema_objects if obj["type"] == "view"),
        "triggers": sorted(obj["name"] for obj in schema_objects if obj["type"] == "trigger"),
        "virtual_tables": sorted(obj["name"] for obj in schema_objects if obj["is_virtual"]),
        "sensitivity": sensitivity,
        "secret_credentials_detected": sensitivity["categories"]["secret_credentials"]["present"],
        "source_classification": source_classification,
        "classification_basis": classification_basis,
        "sidecars_before": len(before_sidecars),
        "sidecars_after": len(after_sidecars),
        "read_only_contract_passed": True,
    }


def _markdown_cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _constraint_labels(column: dict) -> str:
    labels = []
    if column["primary_key_position"]:
        labels.append(f"PK#{column['primary_key_position']}")
    if column["not_null"]:
        labels.append("NOT NULL")
    if column["has_default"]:
        labels.append("DEFAULT(值不展示)")
    if column["hidden"]:
        labels.append(f"hidden={column['hidden']}")
    return ", ".join(labels) or "无"


def _render_audit_structure(audit: dict) -> list[str]:
    lines = [
        "## 结构与完整性",
        "",
        f"- `integrity_check`: `{audit['integrity_check']}`",
        f"- `user_version`: `{audit['user_version']}`",
        f"- `sqlite_schema` 对象数: `{len(audit['schema_objects'])}`",
        f"- 触发器数: `{len(audit['triggers'])}`；视图数: `{len(audit['views'])}`；虚拟表数: `{len(audit['virtual_tables'])}`",
        "",
    ]
    for table in audit["tables"]:
        lines.extend(
            [
                f"### 表 `{_markdown_cell(table['name'])}`",
                "",
                f"- 行数: `{table['row_count']}`",
                f"- 外键数: `{table['foreign_key_count']}`；索引数: `{len(table['indexes'])}`；CHECK约束数: `{table['check_constraint_count']}`",
                f"- 虚拟表: `{'是' if table['is_virtual'] else '否'}`",
                "",
                "| 列名 | 类型 | 约束 |",
                "| --- | --- | --- |",
            ]
        )
        for column in table["columns"]:
            lines.append(
                "| {name} | {type_name} | {constraints} |".format(
                    name=_markdown_cell(column["name"]),
                    type_name=_markdown_cell(column["type"] or "未声明"),
                    constraints=_markdown_cell(_constraint_labels(column)),
                )
            )
        lines.append("")
    if audit["views"]:
        lines.append("- 视图: " + ", ".join(f"`{_markdown_cell(item)}`" for item in audit["views"]))
    if audit["triggers"]:
        lines.append("- 触发器: " + ", ".join(f"`{_markdown_cell(item)}`" for item in audit["triggers"]))
    if audit["virtual_tables"]:
        lines.append("- 虚拟表: " + ", ".join(f"`{_markdown_cell(item)}`" for item in audit["virtual_tables"]))
    lines.append("")
    return lines


def render_public_report(
    *, artifact_identities: list[dict], audit: dict, generated_on: str
) -> str:
    """Render a value-free public report, unless credentials were detected."""
    if audit.get("secret_credentials_detected"):
        raise SecretCredentialDetected(
            "confirmed credential material detected; public report generation stopped"
        )

    identical = len({item["sha256"] for item in artifact_identities}) <= 1
    lines = [
        "# 跟踪 SQLite 备份只读审计（2026-08-25）",
        "",
        f"实际执行日期: `{generated_on}`。本报告只列结构、计数、哈希与风险类别，不复制任何数据库字段值。",
        "",
        "## 范围",
        "",
        f"`git ls-files` 共发现 `{len(artifact_identities)}` 个 SQLite 类制品。",
        "",
        "| 路径 | Git blob | SHA-256 | 字节数 | 首次引入提交 | 最后修改提交 |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for item in artifact_identities:
        lines.append(
            "| `{path}` | `{blob}` | `{sha}` | {size} | `{first}` | `{last}` |".format(
                path=_markdown_cell(item["path"]),
                blob=item["git_blob"],
                sha=item["sha256"],
                size=item["bytes"],
                first=item["first_commit"],
                last=item["last_commit"],
            )
        )
    lines.extend(
        [
            "",
            (
                "三份文件逐字节一致，因此内容只审计一份；所有路径仍单独列示。"
                if identical and len(artifact_identities) > 1
                else "文件内容不完全一致，每个唯一哈希都应独立审计。"
            ),
            "",
            "## 只读方法",
            "",
            "- SQLite URI 使用 `mode=ro&immutable=1`。",
            "- 连接后立即设置 `PRAGMA query_only=ON`，并由 `closing()` 显式关闭。",
            "- 审计前后核对 SHA-256、纳秒级 mtime 与 `-wal/-shm/-journal` sidecar 状态。",
            f"- 合同结果: `{'通过' if audit['read_only_contract_passed'] else '失败'}`；审计前后 sidecar 数 `{audit['sidecars_before']} -> {audit['sidecars_after']}`。",
            "",
        ]
    )
    lines.extend(_render_audit_structure(audit))
    lines.extend(
        [
            "## 敏感性分类",
            "",
            "内部扫描检查可疑列名及邮箱、电话、高熵串、路线/日期、订阅约束与强凭据模式；这里只公开是否存在和计数。",
            "",
            "| 字段类别 | 是否存在 | 命中行数 | 命中列数 | 风险等级 |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for key in (
        "secret_credentials",
        "direct_personal_information",
        "personal_itinerary_metadata",
        "anonymous_market_observations",
        "high_entropy_identifier",
    ):
        finding = audit["sensitivity"]["categories"][key]
        lines.append(
            f"| {CATEGORY_LABELS[key]} | {'是' if finding['present'] else '否'} | {finding['matched_rows']} | {finding['matched_columns']} | {finding['risk']} |"
        )
    lines.extend(
        [
            "",
            "| 内容模式 | 是否存在 | 命中行数 |",
            "| --- | --- | ---: |",
        ]
    )
    pattern_labels = {
        "email_style": "邮箱样式",
        "phone_style": "电话样式",
        "high_entropy_string": "高熵字符串",
        "route_or_date_record": "路线或日期记录",
        "constraint_structure": "订阅约束结构",
        "secret_credential_pattern": "强凭据模式",
    }
    for key, label in pattern_labels.items():
        finding = audit["sensitivity"]["patterns"][key]
        lines.append(f"| {label} | {'是' if finding['present'] else '否'} | {finding['matched_rows']} |")
    lines.extend(
        [
            "",
            "## 来源判定",
            "",
            f"- 分类: **{audit['source_classification']}**。",
            f"- 依据: {audit['classification_basis']}。",
            "- 该分类不以“未发现邮箱”推断无风险；数据库缺少直接身份键，也不能证明路线与日期和个人计划无关。",
            "",
            "## 推荐处置",
            "",
            "- 本审计不移动、不删除任何制品，也不改写 Git 历史。",
            "- 若所有者确认这些快照承载个人行程元数据，比例适当的后续方案是：从当前分支删除三份跟踪文件、保留 `.gitignore` 防线、**不改写历史**。历史仍可见是已知残余风险，是否接受由所有者决定。",
            "- 若日后证明内容纯合成，也不建议保留三份相同二进制；更干净的方向是运行时生成或使用 `.sql`/`.json` fixture。该转换不属于本笔。",
            "- 只有发现真实秘密凭据时才值得优先讨论轮换与历史清理；本报告生成本身即证明未触发秘密凭据硬闸。",
            "",
            "## 已知限制",
            "",
            "- 内容模式扫描用于风险分级，不是法证级凭据发现器，也不能证明数据主体身份。",
            "- Git blob 与提交记录只覆盖当前可达历史；远端缓存、fork 与第三方镜像不在本地审计范围。",
            "- `immutable=1` 适用于静态备份审计；生产数据库未被本脚本读取或写入。",
            "",
        ]
    )
    return "\n".join(lines)


def run_audit(repo_root: str | Path, output: str | Path, generated_on: str) -> dict:
    root = Path(repo_root).resolve()
    tracked = discover_tracked_sqlite_artifacts(root)
    identities = [artifact_identity(root, relative_path) for relative_path in tracked]
    if not identities:
        raise RuntimeError("git ls-files found no tracked SQLite artifacts")
    unique_hashes = defaultdict(list)
    for identity in identities:
        unique_hashes[identity["sha256"]].append(identity)
    if len(unique_hashes) != 1:
        raise RuntimeError(
            "tracked SQLite artifacts are not byte-identical; audit each hash separately"
        )

    audit = audit_sqlite_file(root / identities[0]["path"])
    report = render_public_report(
        artifact_identities=identities,
        audit=audit,
        generated_on=generated_on,
    )
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8", newline="\n")
    return {
        "tracked_count": len(identities),
        "unique_content_count": len(unique_hashes),
        "canonical_path": identities[0]["path"],
        "classification": audit["source_classification"],
        "output": str(output_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Strictly read-only audit of SQLite artifacts tracked by Git."
    )
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument(
        "--output",
        default="docs/tracked-sqlite-backup-audit-2026-08-25.md",
    )
    parser.add_argument("--generated-on", default="2026-08-26")
    args = parser.parse_args(argv)
    try:
        result = run_audit(args.repo_root, args.output, args.generated_on)
    except SecretCredentialDetected:
        print(
            "[审计阻断] 检测到可能的秘密凭据；未生成公开报告。请先私下轮换并复核。",
            file=sys.stderr,
        )
        return 2
    print(
        "[只读审计] tracked={tracked_count} unique={unique_content_count} "
        "classification={classification} output={output}".format(**result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
