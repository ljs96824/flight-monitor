"""R31：三处原地改写的 JSON 文件改为先写同目录临时文件、再替换。

基线(R30 合并后的 main，d55a7930，tree b8d15312)：request_cache._write_persistent(持久请求缓存)、
main._collect_same_day_fallback_flights 写的当天往返备选缓存、plan_tracker.save_pushed_plans(已推送方案)直接
Path.write_text——先截断原文件再写，写到一半失败(如磁盘已满)时原文件只剩已写出的部分(本测试里为 0 字节)，上一份缓存或
推送记录随之丢失：持久缓存读回按未命中(R27)，已推送方案读回按没有记录(R26)。
口径：三处改用 atomic_json_store.write_text_atomic——先写同目录临时文件并落盘，再替换；写到一半失败时原文件字节不变、
临时文件随即删除，异常照旧(持久缓存记一行并停用该目录到本轮结束，另两处由调用方接住)；成功时写出的字节与原先
Path.write_text 相同。写入失败用"打开写入后第一次写就报磁盘已满"模拟；持久缓存另测替换一直被拒(Windows 上文件被占用，
重试后仍失败)。每例结束时核对目录里只剩应有的文件。本机时钟固定。
"""

from contextlib import redirect_stdout
from datetime import datetime
import errno
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import atomic_json_store
import main
import plan_tracker
import request_cache
from tests import test_return_phase_processing as rp


NOW = datetime(2026, 11, 6, 21, 30, 0)
PERSIST_KEY = ("juhe", "PVG", "KIX", "2026-12-01", "1", "economy")
REAL_OPEN = Path.open


class FixedDateTime(datetime):
    """固定本机时钟：被测模块的 datetime.now() 一律返回 NOW(测试不读宿主时钟)。"""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


class _FullDiskStream:
    def __init__(self, stream):
        self._stream = stream

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._stream.close()
        return False

    def write(self, text):
        raise OSError(errno.ENOSPC, "No space left on device")

    def __getattr__(self, name):
        return getattr(self._stream, name)


def disk_full():
    """之后以写入方式打开的文件，第一次写就报磁盘已满：文件已被打开(原有内容已截断或新建)，一个字节都没写进去。"""

    def opener(path, mode="r", *args, **kwargs):
        stream = REAL_OPEN(path, mode, *args, **kwargs)
        return _FullDiskStream(stream) if "w" in mode else stream

    return patch.object(Path, "open", opener)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)

    def files(self):
        """临时目录里的全部文件(相对路径)：临时文件不论叫什么名字，残留都会出现在这里。"""

        return sorted(path.relative_to(self.dir).as_posix() for path in self.dir.rglob("*") if path.is_file())

    def only(self, path):
        return [path.relative_to(self.dir).as_posix()]

    def write_text_bytes(self, text):
        """原先 Path.write_text(text, encoding="utf-8") 写出的字节(文本模式，按平台转换换行)。"""

        reference = self.dir / "reference.txt"
        reference.write_text(text, encoding="utf-8")
        data = reference.read_bytes()
        reference.unlink()
        return data


class PersistentCacheAtomicWriteTest(TempDirTest):
    def setUp(self):
        super().setUp()
        request_cache.reset_for_tests(self.dir / "cache")
        self.addCleanup(request_cache.reset_for_tests, None)
        self.enterContext(patch.object(request_cache, "datetime", FixedDateTime))
        self.log = self.enterContext(patch.object(request_cache, "safe_log"))
        self.path = request_cache._cache_path(PERSIST_KEY)

    def result(self, price):
        return {"source": "juhe", "source_status": "success", "flights": [{"flight_no": "MU521", "price": price, "airline_name": "东方航空"}]}

    def test_success_writes_the_same_bytes_and_leaves_no_temporary_file(self):
        request_cache._write_persistent(PERSIST_KEY, self.result(1280))
        payload = {"fetched_at": "2026-11-06T21:30:00", "key": list(PERSIST_KEY), "producer_class": "", "result": self.result(1280)}
        self.assertEqual(self.path.read_bytes(), self.write_text_bytes(json.dumps(payload, ensure_ascii=False)))
        self.assertEqual(self.files(), self.only(self.path))
        self.log.assert_not_called()

    def test_failed_write_keeps_the_previous_cache_file(self):
        request_cache._write_persistent(PERSIST_KEY, self.result(1280))
        before = self.path.read_bytes()
        with disk_full():
            request_cache._write_persistent(PERSIST_KEY, self.result(990))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.files(), self.only(self.path))
        self.assertEqual([call.args[0] for call in self.log.call_args_list], [
            f"[缓存] 持久化失败,本轮将跳过该目录 {self.path.parent}: [Errno {errno.ENOSPC}] No space left on device"])
        self.assertEqual(request_cache._read_persistent_payload(PERSIST_KEY)["result"], self.result(1280))

    def test_replace_refused_after_retries_keeps_the_previous_cache_file(self):
        request_cache._write_persistent(PERSIST_KEY, self.result(1280))
        before = self.path.read_bytes()
        refused = PermissionError(errno.EACCES, "Access is denied")
        with patch.object(atomic_json_store.os, "replace", side_effect=refused) as replace, \
                patch.object(atomic_json_store.time, "sleep") as sleep:
            request_cache._write_persistent(PERSIST_KEY, self.result(990))
        self.assertEqual([Path(call.args[1]) for call in replace.call_args_list],
                         [self.path] * (atomic_json_store.ATOMIC_REPLACE_RETRIES + 1))
        self.assertEqual(sleep.call_count, atomic_json_store.ATOMIC_REPLACE_RETRIES)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.files(), self.only(self.path))
        self.assertEqual([call.args[0] for call in self.log.call_args_list], [
            f"[缓存] 持久化失败,本轮将跳过该目录 {self.path.parent}: [Errno {errno.EACCES}] Access is denied"])
        self.assertEqual(request_cache._read_persistent_payload(PERSIST_KEY)["result"], self.result(1280))


class FallbackCacheAtomicWriteTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.dir / "same_day_fallback.json"
        self.enterContext(patch.object(main, "datetime", FixedDateTime))
        self.enterContext(patch.object(main, "_fallback_cache_path", return_value=self.path))

    def collect(self):
        with patch.object(main, "collect_for_airport_matrix", return_value={"flights": [rp.flight(True, 1300)]}), \
                redirect_stdout(io.StringIO()):
            return main._collect_same_day_fallback_flights(None, ["PVG"], ["KIX"], "2099-10-01")

    def test_success_writes_the_same_bytes_and_leaves_no_temporary_file(self):
        flights = self.collect()
        payload = {"updated_at": "2026-11-06T21:30:00", "date": "2099-10-01", "flights": flights}
        self.assertEqual([flight["flight_combo"] for flight in flights], ["MU101"])
        self.assertEqual(self.path.read_bytes(),
                         self.write_text_bytes(json.dumps(payload, ensure_ascii=False, indent=2, default=str)))
        self.assertEqual(self.files(), self.only(self.path))

    def test_failed_write_keeps_the_previous_stale_file(self):
        stale = json.dumps({"updated_at": "2026-11-05T09:30:00", "date": "2099-10-01", "flights": []}).encode("utf-8")
        self.path.write_bytes(stale)
        with disk_full(), self.assertRaises(OSError) as caught:
            self.collect()
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(self.path.read_bytes(), stale)
        self.assertEqual(self.files(), self.only(self.path))


class PushedPlansAtomicWriteTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(plan_tracker, "datetime", FixedDateTime))
        self.store = self.dir / "pushed_plans"
        self.path = plan_tracker._storage_path("sub-r31", self.store)

    def save(self, price):
        plan = {"is_roundtrip": True, "outbound_flight": {"flight_no": "MU101", "price": price},
                "return_flight": {"flight_no": "MU102", "price": 900}, "price": price + 900}
        return plan_tracker.save_pushed_plans("sub-r31", [plan], self.store)

    def test_success_writes_the_same_bytes_and_leaves_no_temporary_file(self):
        saved = self.save(1000)
        self.assertEqual(self.path.read_bytes(), self.write_text_bytes(json.dumps(saved, ensure_ascii=False, indent=2)))
        self.assertEqual(self.files(), self.only(self.path))

    def test_failed_write_keeps_the_previous_record(self):
        saved = self.save(1000)
        before = self.path.read_bytes()
        with disk_full(), self.assertRaises(OSError) as caught:
            self.save(800)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.files(), self.only(self.path))
        self.assertEqual(plan_tracker.load_pushed_plans("sub-r31", self.store), saved)


if __name__ == "__main__":
    unittest.main()
