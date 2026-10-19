"""R9-1：本地订阅文件缺失时，运行入口明确失败，同步不得把它重建。

基线(R8-1 合并后的 main)：main.load_file_subscriptions 在 data/subscriptions.json 不存在时返回 []，本轮只打印"暂无订阅"；
_run_locked 先做 PA 同步(异常被吞掉、继续)、再 init_db、最后加载；sync_subscriptions 写入时把缺失的本地文件当作空集合，
用 PA 列表重建出一份新文件；python main.py 与 python sync_subscriptions.py 都以退出码 0 结束。文件被误删或恢复出错时，
监控要么静默停摆，要么被一份只含 PA 条目的新文件悄悄顶替。
口径(R9 定案)：两个命令入口在同步与 init_db 之前确认订阅文件存在；缺失即记一行 [订阅输入缺失]，不同步、不初始化、不采集，
以退出码 2 结束，不创建文件。共享函数只抛 SubscriptionInputMissing，不结束进程。同步在下载前确认本地文件存在；写入阶段发现
文件已消失同样抛错，不按空集合重建。合法的 [] 仍视为暂无订阅；文件损坏或不是数组仍按既有规则显式报错。首次使用由
scripts/initialize_subscriptions.py 在文件锁内显式写入 []，文件已存在(含损坏)时拒绝覆盖。本笔不发运维告警、不做恢复。
"""

import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import main
import sync_subscriptions as sync
from atomic_json_store import JsonStoreReadError
from scripts import initialize_subscriptions as init_script


ROOT = Path(__file__).resolve().parents[1]


REMOTE = [{"subscription_id": "123e4567-e89b-12d3-a456-426614174090", "origin": "上海", "destination": "大阪",
           "depart_date": "2026-12-01"}]


def names(root: Path):
    return sorted(path.name for path in root.iterdir())


class RunEntryTest(unittest.TestCase):
    def run_cli(self, root: Path, *, sync_remote=True):
        logs = []
        effects = {name: Mock(name=name) for name in ("sync_subscriptions", "init_db", "run")}
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "SUBSCRIPTIONS_PATH", root / "subscriptions.json"))
            for name, mock in effects.items():
                stack.enter_context(patch.object(main, name, mock))
            stack.enter_context(patch.object(main, "safe_log", side_effect=logs.append))
            code = main.run_cli(sync_remote=sync_remote)
        return code, effects, logs

    def test_missing_file_stops_before_sync_and_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, effects, logs = self.run_cli(root)
            created = names(root)
        self.assertEqual(code, 2)
        for name, mock in effects.items():
            self.assertEqual(mock.call_count, 0, f"{name}_not_called")
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0].startswith("[订阅输入缺失] 订阅文件缺失: "))
        self.assertIn("不同步、不初始化、不采集", logs[0])
        self.assertEqual(created, [], "nothing_created")

    def test_existing_file_runs_normally(self):
        for content in ("[]", json.dumps(REMOTE, ensure_ascii=False)):
            with self.subTest(content=content[:8]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "subscriptions.json").write_text(content, encoding="utf-8")
                code, effects, logs = self.run_cli(root, sync_remote=False)
                self.assertEqual((code, logs), (0, []))
                effects["run"].assert_called_once_with(sync_remote=False)

    def test_file_vanishing_during_run_is_still_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "subscriptions.json").write_text("[]", encoding="utf-8")
            logs = []
            with (
                patch.object(main, "SUBSCRIPTIONS_PATH", root / "subscriptions.json"),
                patch.object(main, "run", side_effect=sync.SubscriptionInputMissing("订阅文件缺失: x")),
                patch.object(main, "safe_log", side_effect=logs.append),
            ):
                code = main.run_cli()
        self.assertEqual(code, 2)
        self.assertTrue(logs[0].startswith("[订阅输入缺失] 订阅文件缺失: x"))

    def test_main_module_entry_exits_with_run_cli_code(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        guard = [node for node in tree.body if isinstance(node, ast.If)
                 and ast.dump(node.test) == ast.dump(ast.parse('__name__ == "__main__"', mode="eval").body)]
        self.assertEqual(len(guard), 1)
        self.assertEqual([ast.dump(item) for item in guard[0].body],
                         [ast.dump(ast.parse("raise SystemExit(run_cli())").body[0])])


class LoadAndLockedRunTest(unittest.TestCase):
    def test_load_file_subscriptions_contract(self):
        cases = (
            ("missing", None, sync.SubscriptionInputMissing),
            ("corrupt", "{bad", JsonStoreReadError),
            ("not_array", '{"a": 1}', ValueError),
            ("empty_array", "[]", None),
        )
        for name, content, error in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "subscriptions.json"
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                with patch.object(main, "SUBSCRIPTIONS_PATH", path), redirect_stdout(io.StringIO()):
                    if error is None:
                        self.assertEqual(main.load_file_subscriptions(), [])
                    else:
                        with self.assertRaises(error):
                            main.load_file_subscriptions()
                self.assertEqual(path.exists(), content is not None, "file_not_created")

    def test_locked_run_does_not_swallow_missing_from_sync(self):
        init_db = Mock()
        with (
            patch.object(main, "sync_subscriptions", side_effect=sync.SubscriptionInputMissing("订阅文件缺失: y")),
            patch.object(main, "init_db", init_db),
            self.assertRaises(sync.SubscriptionInputMissing),
        ):
            main._run_locked(sync_remote=True, round_id="collection_synthetic")
        init_db.assert_not_called()

    def test_empty_array_keeps_no_subscription_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subscriptions.json"
            path.write_text("[]", encoding="utf-8")
            sentinel = Mock()
            with (
                patch.object(main, "SUBSCRIPTIONS_PATH", path),
                patch.object(main, "init_db"),
                patch.object(main, "_run_basket_sentinel_for_main", sentinel),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = main._run_locked(sync_remote=False, round_id="collection_synthetic")
        self.assertIsNone(result)
        self.assertIn("暂无订阅，请通过表单添加", output.getvalue())
        sentinel.assert_called_once_with([])


class SyncEntryTest(unittest.TestCase):
    def test_sync_refuses_missing_local_before_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            download = Mock(return_value=REMOTE)
            with (
                patch.object(sync, "LOCAL_SUBSCRIPTIONS", root / "subscriptions.json"),
                patch.object(sync, "download_remote_subscriptions", download),
                self.assertRaises(sync.SubscriptionInputMissing),
            ):
                sync.sync_subscriptions()
            created = names(root)
        download.assert_not_called()
        self.assertEqual(created, [])

    def test_file_vanishing_after_check_is_not_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "subscriptions.json"
            local.write_text("[]", encoding="utf-8")

            def download_then_vanish():
                local.unlink()
                return REMOTE

            with (
                patch.object(sync, "LOCAL_SUBSCRIPTIONS", local),
                patch.object(sync, "download_remote_subscriptions", side_effect=download_then_vanish),
                self.assertRaises(sync.SubscriptionInputMissing),
            ):
                sync.sync_subscriptions()
            exists = local.exists()
        self.assertFalse(exists, "not_rebuilt_from_remote")

    def test_normal_only_add_sync_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "subscriptions.json"
            local.write_text("[]", encoding="utf-8")
            with (
                patch.object(sync, "LOCAL_SUBSCRIPTIONS", local),
                patch.object(sync, "download_remote_subscriptions", return_value=REMOTE),
                patch.object(sync, "safe_log"),
            ):
                result = sync.sync_subscriptions()
            saved = json.loads(local.read_text(encoding="utf-8"))
        self.assertEqual((result["synced"], result["added"], result["total"]), (True, 1, 1))
        self.assertEqual(saved[0]["subscription_id"], REMOTE[0]["subscription_id"])

    def test_sync_script_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            download = Mock(return_value=REMOTE)
            run_cli = Mock(return_value=0)
            logs = []
            with (
                patch.object(sync, "LOCAL_SUBSCRIPTIONS", root / "subscriptions.json"),
                patch.object(sync, "download_remote_subscriptions", download),
                patch.object(sync, "safe_log", side_effect=logs.append),
                patch.object(main, "run_cli", run_cli),
            ):
                missing_code = sync.main()
                (root / "subscriptions.json").write_text("[]", encoding="utf-8")
                present_code = sync.main()
        self.assertEqual((missing_code, present_code), (2, 0))
        self.assertTrue(logs[0].startswith("[订阅输入缺失] 订阅文件缺失: "))
        self.assertEqual(download.call_count, 1, "downloaded_only_when_present")
        run_cli.assert_called_once_with(sync_remote=False)
        tree = ast.parse((ROOT / "sync_subscriptions.py").read_text(encoding="utf-8-sig"))
        guard = [node for node in tree.body if isinstance(node, ast.If)]
        self.assertEqual([ast.dump(item) for item in guard[-1].body],
                         [ast.dump(ast.parse("raise SystemExit(main())").body[0])])


class InitializeSubscriptionsTest(unittest.TestCase):
    def run_script(self, path: Path):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = init_script.main(["--path", str(path)])
        return code, out.getvalue(), err.getvalue()

    def test_creates_empty_array_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "subscriptions.json"
            code, out, _err = self.run_script(path)
            first = path.read_bytes()
            again, _out, err = self.run_script(path)
            second = path.read_bytes()
            leftovers = [name for name in names(path.parent) if name.endswith(".tmp")]
        self.assertEqual((code, json.loads(first.decode("utf-8"))), (0, []))
        self.assertIn("[订阅初始化] 完成", out)
        self.assertEqual(again, 2)
        self.assertIn("[订阅初始化] 拒绝覆盖", err)
        self.assertEqual(second, first)
        self.assertEqual(leftovers, [])

    def test_refuses_existing_content_including_corrupt(self):
        for content in (b"[]", b'[{"subscription_id": "keep"}]', b"{bad"):
            with self.subTest(content=content[:8]), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "subscriptions.json"
                path.write_bytes(content)
                code, _out, err = self.run_script(path)
                self.assertEqual(code, 2)
                self.assertIn("拒绝覆盖", err)
                self.assertEqual(path.read_bytes(), content)

    def test_check_and_write_happen_under_the_shared_file_lock(self):
        held = []
        real_lock = init_script.file_lock

        def tracking_lock(target, *args, **kwargs):
            manager = real_lock(target, *args, **kwargs)

            class Tracking:
                def __enter__(self_inner):
                    manager.__enter__()
                    held.append(Path(target))
                    return self_inner

                def __exit__(self_inner, *exc):
                    held.append("released")
                    return manager.__exit__(*exc)

            return Tracking()

        writes = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subscriptions.json"
            with (
                patch.object(init_script, "file_lock", side_effect=tracking_lock),
                patch.object(init_script, "write_text_atomic",
                             side_effect=lambda target, text: writes.append((list(held), text))),
            ):
                init_script.initialize_subscriptions_file(path)
        self.assertEqual(writes, [([path], "[]")], "write_inside_lock")
        self.assertEqual(held, [path, "released"])


if __name__ == "__main__":
    unittest.main()
