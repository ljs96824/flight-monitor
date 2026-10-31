"""R21：同步器对本地订阅文件的分类——确实不存在才是"缺失"，内容为 JSON null 与其他非数组内容一样是"格式错误"，与远端有无记录无关。

基线(R20 合并后的 main，b622a1a4，tree a04a9041)：
- sync_subscriptions.sync_subscriptions 在远端为空时经 _load_json_list 读取本地文件，null 报"subscriptions.json 格式错误，应为订阅数组"
  (ValueError)；远端有记录时走 update_json 的写入，而 update_json 对"文件不存在"与"现有内容为 null"都向 mutator 传 None，mutator
  把 None 一律当作缺失，抛 SubscriptionInputMissing("订阅文件缺失")。同一个 null 文件的分类因此取决于远端有没有记录：经
  main.run_cli(计划任务入口)远端有记录时以退出码 2 结束、记"[订阅输入缺失]"并发"订阅输入缺失"运维告警，提示去执行
  scripts/initialize_subscriptions.py，而该脚本对已存在的文件(含损坏)一律拒绝覆盖。
- 远端为空时，若文件在下载期间消失，_load_json_list 把它当作空列表(total=0)正常返回；计划任务入口随后先执行一次 init_db，
  再在加载时以退出码 2 结束，与 README"不同步、不初始化、不采集"不符。
- sync_subscriptions.py 自身入口只把开头的存在检查放在 try 里，sync_subscriptions() 在 try 之外：同步途中文件消失时异常不被
  捕获(退出码 1、不告警)，或在远端为空时照常进入采集，与 README"缺失即退出码 2"不符。
口径(用户 2026-10-30"下一步开工"，按建议口径)：
- 同步写入改用 update_json 的 missing 参数(R16 机制)，传入 JSON 解析不可能产出的缺文件标记，在同一把锁内区分：只有文件确实
  不存在才抛 SubscriptionInputMissing；现有内容为 null 与其他非数组内容一样抛 ValueError("subscriptions.json 格式错误，应为订阅数组")，
  不写入、字节不变——与远端为空时的读取、main.load_file_subscriptions 同一口径(R9-1"文件损坏或不是数组仍按既有规则显式报错")。
- 远端为空时的读取同样区分：文件已消失抛 SubscriptionInputMissing，不再当作空列表(独立复核后补)。
- sync_subscriptions.py 自身入口把 sync_subscriptions() 纳入缺失处理：同步途中文件消失与开头即缺失一样记一行"[订阅输入缺失]"、
  尝试告警、以退出码 2 结束、不进入采集；格式错误等其他异常照旧向上抛(退出码 1)。
- 由此带来的变化：null 文件不再触发"订阅输入缺失"告警与退出码 2；两个入口对 null 一律以格式错误失败(退出码 1)，与远端为空时、
  与其他非数组内容相同，不发告警。main._run_locked 吞掉同步异常、继续处理本地订阅的既有行为不变；PA 网页端订阅仓库不在本笔。
"""

from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import atomic_json_store
import main
import sync_subscriptions as sync


REMOTE = [{"subscription_id": "123e4567-e89b-12d3-a456-426614174091", "origin": "上海", "destination": "大阪",
           "depart_date": "2026-12-01"}]
FORMAT_ERROR = "subscriptions.json 格式错误，应为订阅数组"
NULL_CONTENTS = (b"null", b"null\r\n", b" null \n")
REMOTES = (("remote_empty", []), ("remote_items", REMOTE))


def temp_files(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.name.endswith(".tmp"))


def vanish_then(local: Path, remote):
    """下载期间文件被删除，再返回远端列表。"""

    def download():
        local.unlink()
        return deepcopy(remote)

    return download


class SyncClassificationTest(unittest.TestCase):
    """sync_subscriptions 本身：缺失与格式错误的分类与远端有无记录无关，两种情况都不写入。"""

    def sync_once(self, local: Path, download):
        with (
            patch.object(sync, "LOCAL_SUBSCRIPTIONS", local),
            patch.object(sync, "download_remote_subscriptions", side_effect=download),
            patch.object(sync, "safe_log"),
        ):
            return sync.sync_subscriptions()

    def test_null_local_is_format_error_whatever_the_remote(self):
        for label, remote in REMOTES:
            for content in NULL_CONTENTS:
                with self.subTest(remote=label, content=content), tempfile.TemporaryDirectory() as tmp:
                    local = Path(tmp) / "subscriptions.json"
                    local.write_bytes(content)
                    with self.assertRaises(ValueError) as caught:
                        self.sync_once(local, lambda remote=remote: deepcopy(remote))
                    self.assertEqual(str(caught.exception), FORMAT_ERROR)
                    self.assertEqual(local.read_bytes(), content, "BYTES_UNCHANGED")
                    self.assertEqual(temp_files(Path(tmp)), [])

    def test_other_non_array_contents_keep_the_same_classification(self):
        for content in (b'{"a": 1}', b"{}", b'"text"', b"0", b"false"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "subscriptions.json"
                local.write_bytes(content)
                with self.assertRaises(ValueError) as caught:
                    self.sync_once(local, lambda: deepcopy(REMOTE))
                self.assertEqual(str(caught.exception), FORMAT_ERROR)
                self.assertEqual(local.read_bytes(), content)

    def test_file_vanishing_during_download_is_missing_whatever_the_remote(self):
        for label, remote in REMOTES:
            with self.subTest(remote=label), tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "subscriptions.json"
                local.write_text("[]", encoding="utf-8")
                with self.assertRaises(sync.SubscriptionInputMissing):
                    self.sync_once(local, vanish_then(local, remote))
                self.assertFalse(local.exists(), "NOT_REBUILT")
                self.assertEqual(temp_files(Path(tmp)), [])

    def test_file_removed_inside_the_lock_is_still_missing(self):
        """缺失与 null 的区分发生在 update_json 的同一把锁内：拿到锁之后文件才消失，仍按缺失处理、不重建(独立复核后补)。"""
        real_lock = atomic_json_store.file_lock

        @contextmanager
        def lock_then_vanish(target, *args, **kwargs):
            with real_lock(target, *args, **kwargs):
                Path(target).unlink()
                yield

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "subscriptions.json"
            local.write_text("[]", encoding="utf-8")
            with patch.object(atomic_json_store, "file_lock", lock_then_vanish):
                with self.assertRaises(sync.SubscriptionInputMissing):
                    self.sync_once(local, lambda: deepcopy(REMOTE))
            self.assertFalse(local.exists(), "NOT_REBUILT")
            self.assertEqual(temp_files(Path(tmp)), [])


class ScheduledEntryTest(unittest.TestCase):
    """main.run_cli(计划任务入口)经真实 _run_locked 与真实 sync_subscriptions，下载与 init_db 换成桩。"""

    def run_scheduled(self, local: Path, download):
        logs, output = [], io.StringIO()
        alert, init_db = Mock(), Mock()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch.object(main, "SUBSCRIPTIONS_PATH", local))
            stack.enter_context(patch.object(sync, "LOCAL_SUBSCRIPTIONS", local))
            stack.enter_context(patch.object(sync, "download_remote_subscriptions", side_effect=download))
            stack.enter_context(patch.object(sync, "safe_log"))
            stack.enter_context(patch.object(main, "alert_subscription_input_missing", alert))
            stack.enter_context(patch.object(main, "safe_log", side_effect=logs.append))
            stack.enter_context(patch.object(main, "init_db", init_db))
            stack.enter_context(patch.object(main.logging, "error"))
            stack.enter_context(patch.object(
                main, "run",
                side_effect=lambda sync_remote: main._run_locked(sync_remote=sync_remote, round_id="collection_synthetic"),
            ))
            try:
                outcome = ("returned", main.run_cli())
            except Exception as exc:  # 入口不捕获的异常原样带出，供断言
                outcome = ("raised", exc)
        return outcome, logs, output.getvalue(), alert, init_db

    def test_null_local_fails_as_format_error_and_never_as_missing(self):
        for label, remote in REMOTES:
            with self.subTest(remote=label), tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "subscriptions.json"
                local.write_bytes(b"null")
                (kind, value), logs, output, alert, init_db = self.run_scheduled(
                    local, lambda remote=remote: deepcopy(remote))
                self.assertEqual(kind, "raised")
                self.assertIs(type(value), ValueError)
                self.assertEqual(str(value), FORMAT_ERROR)
                self.assertIn(f"[sync] PythonAnywhere 订阅同步失败，继续处理本地订阅: {FORMAT_ERROR}", output)
                init_db.assert_called_once_with()
                alert.assert_not_called()
                self.assertEqual([line for line in logs if line.startswith("[订阅输入缺失]")], [])
                self.assertEqual(local.read_bytes(), b"null")

    def test_file_vanishing_during_sync_stops_before_init_db(self):
        for label, remote in REMOTES:
            with self.subTest(remote=label), tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "subscriptions.json"
                local.write_text("[]", encoding="utf-8")
                outcome, logs, _output, alert, init_db = self.run_scheduled(local, vanish_then(local, remote))
                self.assertEqual(outcome, ("returned", sync.EXIT_SUBSCRIPTIONS_MISSING))
                self.assertTrue(logs[0].startswith(f"[订阅输入缺失] 订阅文件缺失: {local}"))
                init_db.assert_not_called()
                alert.assert_called_once_with(local)
                self.assertFalse(local.exists(), "NOT_REBUILT")


class SyncScriptEntryTest(unittest.TestCase):
    """sync_subscriptions.py 自身入口：null 报格式错误、不进入采集；同步途中文件消失按缺失以退出码 2 结束。"""

    def run_entry(self, local: Path, download):
        logs = []
        run_cli = Mock(return_value=0)
        alert = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sync, "LOCAL_SUBSCRIPTIONS", local))
            stack.enter_context(patch.object(sync, "download_remote_subscriptions", side_effect=download))
            stack.enter_context(patch.object(sync, "safe_log", side_effect=logs.append))
            stack.enter_context(patch.object(sync, "alert_subscription_input_missing", alert))
            stack.enter_context(patch.object(main, "run_cli", run_cli))
            try:
                outcome = ("returned", sync.main())
            except Exception as exc:  # 入口不捕获的异常原样带出，供断言
                outcome = ("raised", exc)
        return outcome, logs, run_cli, alert

    def test_null_local_raises_format_error_without_collecting(self):
        for label, remote in REMOTES:
            with self.subTest(remote=label), tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "subscriptions.json"
                local.write_bytes(b"null")
                (kind, value), logs, run_cli, alert = self.run_entry(local, lambda remote=remote: deepcopy(remote))
                self.assertEqual(kind, "raised")
                self.assertIs(type(value), ValueError)
                self.assertEqual(str(value), FORMAT_ERROR)
                run_cli.assert_not_called()
                alert.assert_not_called()
                self.assertEqual([line for line in logs if line.startswith("[订阅输入缺失]")], [])
                self.assertEqual(local.read_bytes(), b"null")

    def test_file_vanishing_during_sync_exits_2_without_collecting(self):
        for label, remote in REMOTES:
            with self.subTest(remote=label), tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "subscriptions.json"
                local.write_text("[]", encoding="utf-8")
                outcome, logs, run_cli, alert = self.run_entry(local, vanish_then(local, remote))
                self.assertEqual(outcome, ("returned", sync.EXIT_SUBSCRIPTIONS_MISSING))
                self.assertTrue(logs[0].startswith(f"[订阅输入缺失] 订阅文件缺失: {local}"))
                self.assertIn("不同步、不初始化、不采集", logs[0])
                alert.assert_called_once_with(local)
                run_cli.assert_not_called()
                self.assertFalse(local.exists(), "NOT_REBUILT")


if __name__ == "__main__":
    unittest.main()
