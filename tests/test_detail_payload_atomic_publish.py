"""R8-1：详情快照的本地原子发布。

基线(172435a)：main._save_result_for_page 用 Path.write_text 直接覆盖 data/payloads/<UUID>.json。write_text 先截断目标文件
再写入，写到一半失败(磁盘满、进程被杀、杀毒软件占用)时，上一份完整详情已被破坏，留下半截 JSON；网页读到坏文件只能显示
"可能还在同步中"，随后的 PA 上传也不会发生。
口径：记录结构、序列化(json.dumps(record, ensure_ascii=False, indent=2, default=str))与正常输出字节都不变，只改发布方式——
新内容完整写入同目录临时文件并落盘，再替换最终文件；替换前任一步失败，上一份详情原样保留、临时文件清除、异常照旧抛出，
且不进入上传。详情是本轮完整生成的快照，不读旧文件：旧详情已损坏也不阻止新快照发布。返回值政策不变(R8-2 另行处理)。
本笔不声明 PA 服务端的写入是原子的。
"""

from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import atomic_json_store
import main


SUB_ID = "123e4567-e89b-12d3-a456-426614174080"
ORIGINAL = b'{\n  "subscription_id": "previous snapshot"\n}'


class Unprintable:
    def __str__(self):
        raise RuntimeError("synthetic serialization failure")


class HalfWriteStream:
    """写入一半后报磁盘满，模拟写盘中途失败。"""

    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        self.stream.write(text[: len(text) // 2])
        self.stream.flush()
        raise OSError(28, "synthetic disk full")

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stream.close()
        return False


class DetailPayloadAtomicPublishTest(unittest.TestCase):
    def save(self, root: Path, html="<p>新快照</p>", payload=None, *, extra=()):
        payloads = root / "payloads"
        target = payloads / f"{SUB_ID}.json"
        seen_at_upload = []
        upload = Mock(side_effect=lambda path: seen_at_upload.append(Path(path).read_bytes()) or True)
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "PAGE_PAYLOADS_DIR", payloads))
            stack.enter_context(patch.object(main, "_upload_payload_to_pythonanywhere", upload))
            stack.enter_context(redirect_stdout(io.StringIO()))
            for item in extra:
                stack.enter_context(item)
            result = main._save_result_for_page(
                SUB_ID, html, payload if payload is not None else {"route": "上海 → 大阪"}
            )["local_saved"]  # R8-2 起返回发布回执
        return result, target, upload, seen_at_upload

    def leftovers(self, root: Path):
        payloads = root / "payloads"
        return sorted(path.name for path in payloads.iterdir() if path.name.endswith(".tmp")) if payloads.exists() else []

    def seed(self, root: Path, data: bytes = ORIGINAL) -> Path:
        target = root / "payloads" / f"{SUB_ID}.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
        return target

    def test_bytes_match_previous_writer_and_upload_sees_final_file(self):
        payload = {"route": "上海 → 大阪", "price": Decimal("1234.5"), "at": datetime(2026, 10, 18, 9, 30),
                   "note": "第一行\n第二行"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, target, upload, seen = self.save(root, html="<p>新快照\r\n含换行</p>", payload=payload)
            published = target.read_bytes()
            record = json.loads(published.decode("utf-8"))
            reference = root / "reference.json"
            reference.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            expected = reference.read_bytes()
            leftovers = self.leftovers(root)
        self.assertIs(result, True)
        self.assertEqual(published, expected, "same_bytes_as_path_write_text")
        self.assertEqual((record["payload"]["price"], record["payload"]["at"]), ("1234.5", "2026-10-18 09:30:00"))
        upload.assert_called_once_with(target)
        self.assertEqual(seen, [published], "upload_reads_completed_file")
        self.assertEqual(leftovers, [])

    def test_corrupt_previous_snapshot_does_not_block_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.seed(root, b'{"subscription_id": "trunc')
            result, target, upload, _seen = self.save(root)
            record = json.loads(target.read_text(encoding="utf-8"))
            leftovers = self.leftovers(root)
        self.assertIs(result, True)
        self.assertEqual((record["subscription_id"], record["html"]), (SUB_ID, "<p>新快照</p>"))
        upload.assert_called_once()
        self.assertEqual(leftovers, [])

    def test_failure_before_replace_keeps_previous_snapshot(self):
        real_open = Path.open

        def half_write_open(self, mode="r", *args, **kwargs):
            stream = real_open(self, mode, *args, **kwargs)
            if "w" in mode and self.parent.name == "payloads":
                return HalfWriteStream(stream)
            return stream

        cases = (
            ("serialization", {"payload": {"bad": Unprintable()}}, (), RuntimeError),
            ("disk_full_mid_write", {}, (patch.object(Path, "open", half_write_open),), OSError),
            ("fsync", {}, (patch.object(atomic_json_store.os, "fsync",
                                        side_effect=OSError(5, "synthetic fsync failure")),), OSError),
            ("replace", {}, (patch.object(atomic_json_store.os, "replace",
                                          side_effect=OSError(5, "synthetic replace failure")),), OSError),
            ("replace_denied_after_retries", {}, (patch.object(atomic_json_store.os, "replace",
                                                               side_effect=PermissionError(13, "synthetic denial")),
                                                  patch.object(atomic_json_store.time, "sleep")), PermissionError),
        )
        for name, kwargs, extra, error in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = self.seed(root)
                with self.assertRaises(error):
                    self.save(root, extra=extra, **kwargs)
                self.assertEqual(target.read_bytes(), ORIGINAL, "previous_snapshot_intact")
                self.assertEqual(self.leftovers(root), [], "temporary_removed")
                self.assertEqual(sorted(path.name for path in target.parent.iterdir()), [target.name])

    def test_failure_does_not_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.seed(root)
            upload = Mock(return_value=True)
            with (
                patch.object(main, "PAGE_PAYLOADS_DIR", root / "payloads"),
                patch.object(main, "_upload_payload_to_pythonanywhere", upload),
                patch.object(atomic_json_store.os, "replace", side_effect=OSError(5, "synthetic replace failure")),
                redirect_stdout(io.StringIO()),
                self.assertRaises(OSError),
            ):
                main._save_result_for_page(SUB_ID, "<p>新快照</p>", {})
        upload.assert_not_called()

    def test_transient_replace_denial_is_retried(self):
        real_replace = atomic_json_store.os.replace
        attempts = []

        def flaky_replace(source, destination):
            attempts.append(destination)
            if len(attempts) == 1:
                raise PermissionError(13, "synthetic transient denial")
            return real_replace(source, destination)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.seed(root)
            result, target, upload, _seen = self.save(root, extra=(
                patch.object(atomic_json_store.os, "replace", side_effect=flaky_replace),
                patch.object(atomic_json_store.time, "sleep"),
            ))
            record = json.loads(target.read_text(encoding="utf-8"))
            leftovers = self.leftovers(root)
        self.assertIs(result, True)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(record["html"], "<p>新快照</p>")
        upload.assert_called_once()
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
