"""R23：新建订阅遇到内容为 JSON null 的 subscriptions.json 时按格式错误拒收，不再覆盖。

基线(R22 合并后的 main，3255ea90，tree eccc7255)：SubscriptionRepository.create 的 mutate 用
_subscription_array(payload, allow_missing=True)，而 update_json 对"文件不存在"与"现有内容为 JSON null"都把 None 交给
mutator(缺省 missing=None)，于是 null 被当成空数组，写回只含新订阅的数组；网页 /subscribe 新建因此返回 302、覆盖文件
并触发后台采集。同一个 null 文件，列表与单条读取按格式错误拒收，编辑、暂停、删除不写入(返回 None/False)，本机同步(R21)
同样按格式错误拒收。
口径(用户 2026-11-01"开工下一步"，按建议口径)：create 改传 missing=[]——文件不存在时照旧新建；现有内容为 null 与其他
非数组内容同属格式错误，抛 ValueError("subscriptions.json 格式错误，应为订阅数组")、不写入；缺失与 null 在 update_json
的同一把锁内区分(独立复核后补测)。网页新建走既有 ValueError 分支，返回 400 表单错误，不触发后台采集与握手记录。
读取、编辑、暂停、删除与网页列表遇 null 的处理照旧(其归类不在本笔)。
"""

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import atomic_json_store
import web_form
from subscription_repository import LOCAL_OWNER_ID, SubscriptionRepository
from web_test_utils import enable_csrf


FORMAT_ERROR = "subscriptions.json 格式错误，应为订阅数组"
EXISTING_ID = "123e4567-e89b-12d3-a456-426614174230"
NULL_SPELLINGS = (b"null", b"null\n", b" \r\n null\t\r\n")
OTHER_NON_ARRAYS = (b"{}", b'{"subscriptions": []}', b'"[]"', b"0", b"false", b"true", b"1.5")


def _subscription(subscription_id=None) -> dict:
    item = {
        "origin": "PVG",
        "destination": "KIX",
        "depart_date": "2026-12-01",
        "round_trip": False,
        "route_type": "international",
        "basic": {"origin": "PVG", "destination": "KIX"},
        "hard_constraints": {"max_budget": 1000},
        "soft_preferences": {},
        "notification_goals": {"method": "both", "email": ""},
    }
    if subscription_id:
        item["subscription_id"] = subscription_id
    return item


def _lock_then(action):
    """换掉 update_json 用的文件锁：拿到真实的锁之后先对目标文件执行 action，再交回调用方。"""
    real_lock = atomic_json_store.file_lock

    @contextmanager
    def lock(target, *args, **kwargs):
        with real_lock(target, *args, **kwargs):
            action(Path(target))
            yield

    return patch.object(atomic_json_store, "file_lock", lock)


class RepositoryCreateNullTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)
        self.path = self.dir / "subscriptions.json"
        self.repository = SubscriptionRepository(self.path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def assert_refused_without_writing(self, raw):
        self.path.write_bytes(raw)
        with self.assertRaises(ValueError) as caught:
            self.repository.create(LOCAL_OWNER_ID, _subscription())
        self.assertEqual(str(caught.exception), FORMAT_ERROR)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_json_null_is_refused_without_writing(self):
        for raw in NULL_SPELLINGS:
            with self.subTest(raw=raw):
                self.assert_refused_without_writing(raw)

    def test_other_non_arrays_are_still_refused(self):
        for raw in OTHER_NON_ARRAYS:
            with self.subTest(raw=raw):
                self.assert_refused_without_writing(raw)

    def test_missing_file_is_still_created_with_the_new_subscription(self):
        saved = self.repository.create(LOCAL_OWNER_ID, _subscription())
        self.assertTrue(saved.get("subscription_id"))
        self.assertEqual(self.saved(), [saved])

    def test_existing_arrays_still_append(self):
        existing = _subscription(EXISTING_ID)
        for name, initial in (("empty", []), ("one", [existing])):
            with self.subTest(case=name):
                self.path.write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
                saved = self.repository.create(LOCAL_OWNER_ID, _subscription())
                self.assertEqual(self.saved(), [*initial, saved])

    def test_null_written_after_the_lock_is_taken_is_refused(self):
        """调用时文件还不存在，拿到锁之后才被写成 null：按锁内读到的内容判定，仍拒收、不写入。"""
        with _lock_then(lambda target: target.write_bytes(b"null\n")):
            with self.assertRaises(ValueError) as caught:
                self.repository.create(LOCAL_OWNER_ID, _subscription())
        self.assertEqual(str(caught.exception), FORMAT_ERROR)
        self.assertEqual(self.path.read_bytes(), b"null\n")
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_file_removed_after_the_lock_is_taken_is_still_created(self):
        """调用时文件是数组，拿到锁之后才消失：按锁内看到的"不存在"照旧新建为单元素数组。"""
        self.path.write_text("[]", encoding="utf-8")
        with _lock_then(lambda target: target.unlink()):
            saved = self.repository.create(LOCAL_OWNER_ID, _subscription())
        self.assertEqual(self.saved(), [saved])

    def test_other_paths_keep_their_null_handling(self):
        reads = (
            ("list_for_owner", lambda: self.repository.list_for_owner(LOCAL_OWNER_ID)),
            ("get", lambda: self.repository.get(LOCAL_OWNER_ID, EXISTING_ID)),
            ("resolve_legacy_index", lambda: self.repository.resolve_legacy_index(LOCAL_OWNER_ID, 0)),
        )
        for name, call in reads:
            with self.subTest(op=name):
                self.path.write_bytes(b"null\n")
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assertEqual(str(caught.exception), FORMAT_ERROR)
                self.assertEqual(self.path.read_bytes(), b"null\n")
        writes = (
            ("update", lambda: self.repository.update(LOCAL_OWNER_ID, EXISTING_ID, {"status": "paused"}), None),
            ("mutate", lambda: self.repository.mutate(LOCAL_OWNER_ID, EXISTING_ID, lambda item: item), None),
            ("delete", lambda: self.repository.delete(LOCAL_OWNER_ID, EXISTING_ID), False),
        )
        for name, call, expected in writes:
            with self.subTest(op=name):
                self.path.write_bytes(b"null\n")
                self.assertIs(call(), expected)
                self.assertEqual(self.path.read_bytes(), b"null\n")


class WebCreateOverNullTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "subscriptions.json"
        self.old_path = web_form.SUBSCRIPTIONS_PATH
        web_form.SUBSCRIPTIONS_PATH = self.path
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()
        enable_csrf(self.client)

    def tearDown(self):
        web_form.SUBSCRIPTIONS_PATH = self.old_path
        self.tmpdir.cleanup()

    def test_create_over_null_is_a_form_error_without_side_effects(self):
        for page in ("full", "quick"):
            with self.subTest(form_page=page):
                self.path.write_bytes(b"null\n")
                with (
                    patch.object(web_form, "build_subscription", return_value=_subscription()),
                    patch.object(web_form, "start_background_collection") as start_mock,
                    patch.object(web_form, "_remember_startup_handshake") as handshake_mock,
                ):
                    response = self.client.post("/subscribe", data={"form_page": page})
                self.assertEqual(response.status_code, 400)
                self.assertIn(FORMAT_ERROR, response.get_data(as_text=True))
                start_mock.assert_not_called()
                handshake_mock.assert_not_called()
                self.assertEqual(self.path.read_bytes(), b"null\n")

    def test_create_on_missing_file_still_saves_then_starts(self):
        observed = []

        def start(background_subscription):
            observed.append(json.loads(self.path.read_text(encoding="utf-8")))
            return {"status": "started", "entrypoint": "web"}

        with (
            patch.object(web_form, "build_subscription", return_value=_subscription()),
            patch.object(web_form, "start_background_collection", side_effect=start) as start_mock,
            patch.object(web_form, "_remember_startup_handshake"),
        ):
            response = self.client.post("/subscribe", data={"form_page": "full"})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(saved), 1)
        self.assertTrue(
            response.headers["Location"].endswith(f"/success?subscription_id={saved[0]['subscription_id']}")
        )
        start_mock.assert_called_once()
        self.assertEqual(observed, [saved], "SAVED_BEFORE_START")


if __name__ == "__main__":
    unittest.main()
