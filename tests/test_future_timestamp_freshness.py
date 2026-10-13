"""未来时间戳·两处：缓存或面板观测的时间晚于当前时刻时，不得视为新鲜。

先例：2026-09-16 提交 47a4197 已让 request_cache._fresh 要求 timedelta(0) <= age < ttl。
本笔把同一下界补到两个各自独立的入口，不抽公共函数，上界(含等号)保持原样：
- main._fresh_cached_flights：当天往返备选缓存，按 updated_at 判断；
- observations_store.load_fresh_observation_snapshot：面板复用，按该键最新一批 observed_at 判断。
最新一批位于未来时，整键本轮不复用面板，不回退到更早的批次。
"""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import main
import observations_store
import request_cache
from project_time import SHANGHAI_TZ
from tests import test_return_phase_processing as rp


NOW = datetime(2026, 3, 1, 20, 0, 0)
AGES = (
    (timedelta(0), True),
    (timedelta(hours=3), True),
    (timedelta(hours=6), True),
    (timedelta(hours=6, seconds=1), False),
    (timedelta(seconds=-1), False),
    (timedelta(days=-1), False),
    (timedelta(days=-3650), False),
)
DEPART = "2099-10-01"


class ControlledDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=SHANGHAI_TZ).astimezone(tz) if tz is not None else NOW


class FallbackCacheFreshnessTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "same_day_fallback.json"
        self.enterContext(patch.object(main, "datetime", ControlledDateTime))

    def write_cache(self, updated_at, combo="CACHED1"):
        self.path.write_text(json.dumps({"updated_at": updated_at.isoformat(timespec="seconds"),
                                         "flights": [{"flight_combo": combo}]}), encoding="utf-8")

    def test_age_bounds(self):
        for age, fresh in AGES:
            with self.subTest(age=int(age.total_seconds())):
                self.write_cache(NOW - age)
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = main._fresh_cached_flights(self.path)
                self.assertEqual(result, [{"flight_combo": "CACHED1"}] if fresh else None)
                self.assertIs("使用缓存" in stdout.getvalue(), fresh, "cache_hit_log")

    def test_future_cache_is_refetched_and_rewritten(self):
        for age, fetches in ((timedelta(hours=1), 0), (timedelta(days=-1), 1)):
            with self.subTest(age=int(age.total_seconds())):
                self.write_cache(NOW - age)
                with (
                    patch.object(main, "_fallback_cache_path", return_value=self.path),
                    patch.object(main, "collect_for_airport_matrix",
                                 return_value={"flights": [rp.flight(True, 1300)]}) as collect,
                    redirect_stdout(io.StringIO()),
                ):
                    flights = main._collect_same_day_fallback_flights(None, ["PVG"], ["KIX"], DEPART)
                self.assertEqual(collect.call_count, fetches)
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                if fetches:
                    self.assertEqual([item["flight_combo"] for item in flights], ["MU101"])
                    self.assertEqual(stored["updated_at"], NOW.isoformat(timespec="seconds"), "cache_rewritten")
                else:
                    self.assertEqual(flights, [{"flight_combo": "CACHED1"}])
                    self.assertEqual(stored["updated_at"], (NOW - age).isoformat(timespec="seconds"))


class PanelSnapshotFreshnessTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "observations.sqlite3"

    def seed(self, observed_at, round_id):
        observations_store.append_observations(
            [{"flight_combo": "MU101", "price": 1000, "airline": "MU", "stops": 0, "duration_min": 150}],
            round_id=round_id, route_type="international", origin_airport="PVG", dest_airport="KIX",
            depart_date=DEPART, cabin_class="economy", source="juhe",
            observed_at=observed_at.isoformat(timespec="seconds"), db_path=self.db_path,
        )

    def snapshot(self, now):
        return observations_store.load_fresh_observation_snapshot(
            source="juhe", origin_airport="PVG", dest_airport="KIX", depart_date=DEPART,
            cabin_class="economy", freshness_hours=6, now=now, db_path=self.db_path,
        )

    def test_age_bounds_across_now_representations(self):
        instant = NOW.replace(tzinfo=SHANGHAI_TZ)
        nows = {"shanghai": instant, "utc": instant.astimezone(timezone.utc), "naive": NOW}
        for index, (age, fresh) in enumerate(AGES):
            self.seed(instant - age, f"round_{index}")
            for label, now in nows.items():
                with self.subTest(age=int(age.total_seconds()), now=label):
                    result = self.snapshot(now)
                    if fresh:
                        self.assertEqual(result["round_id"], f"round_{index}")
                        self.assertEqual([row["flight_combo"] for row in result["rows"]], ["MU101"])
                    else:
                        self.assertIsNone(result)
            self.db_path.unlink()

    def test_future_latest_batch_blocks_key(self):
        instant = NOW.replace(tzinfo=SHANGHAI_TZ)
        self.seed(instant - timedelta(hours=1), "round_past")
        self.assertEqual(self.snapshot(instant)["round_id"], "round_past")
        self.seed(instant + timedelta(days=1), "round_future")
        self.assertIsNone(self.snapshot(instant), "future_latest_not_reused")


class SyntheticListingSource:
    name = "juhe"
    route_type = "international"

    def __init__(self):
        self.calls = 0

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls += 1
        return {"source": self.name, "source_status": "success",
                "flights": [{"flight_combo": "MU101", "flight_no": "MU101", "price": 1000,
                             "departure_airport": origin, "arrival_airport": dest,
                             "duration_min": 150}]}


class PanelReuseCallerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.db_path = root / "observations.sqlite3"
        request_cache.reset_for_tests(root / "cache")
        self.addCleanup(request_cache.reset_for_tests, None)
        self.addCleanup(observations_store.clear_current_round)

    def reuse_after_shift(self, seconds):
        source = SyntheticListingSource()
        observations_store.set_current_round("seed_round", self.db_path)
        request_cache.start_request_cache_round("seed_round")
        with redirect_stdout(io.StringIO()):
            request_cache.cached_fetch(source, "PVG", "KIX", DEPART, {"adult": 1}, "economy",
                                       force_fresh=True, persist=True)
        connection = sqlite3.connect(self.db_path)
        try:
            (observed_at,) = connection.execute("SELECT observed_at FROM observations").fetchone()
            shifted = datetime.fromisoformat(observed_at) + timedelta(seconds=seconds)
            connection.execute("UPDATE observations SET observed_at=?", (shifted.isoformat(timespec="seconds"),))
            connection.commit()
        finally:
            connection.close()
        observations_store.set_current_round("subscription_round", self.db_path)
        with redirect_stdout(io.StringIO()):
            result = request_cache.panel_reuse_result(source, "PVG", "KIX", DEPART, {"adult": 1}, "economy",
                                                      freshness_hours=6)
        self.assertEqual(source.calls, 1)
        return result

    def test_recent_panel_still_reused(self):
        result = self.reuse_after_shift(-30)
        self.assertEqual(result["collection_state"], "panel_reused")

    def test_future_panel_within_rebuild_tolerance_not_reused(self):
        self.assertIsNone(self.reuse_after_shift(30), "future_panel_not_reused")


if __name__ == "__main__":
    unittest.main()
