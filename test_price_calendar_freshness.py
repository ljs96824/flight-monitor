import json
import re
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from project_time import SHANGHAI_TZ


class _EmptySource:
    name = "juhe"

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        return {"flights": []}


class _FailingSource:
    name = "juhe"

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        raise PermissionError(13, "calendar cache denied")


def _legacy_success(price: float, *, updated_at: str) -> dict:
    return {
        "min_price": price,
        "airline": "MU",
        "flight_no": "MU225",
        "count": 1,
        "sources": ["juhe"],
        "updated_at": updated_at,
    }


class PriceCalendarFreshnessTest(unittest.TestCase):
    FIXED_TODAY = date(2026, 8, 28)
    NOW = datetime.combine(
        FIXED_TODAY, time(9), tzinfo=SHANGHAI_TZ
    )
    DATE = FIXED_TODAY + timedelta(days=2)

    def _write_calendar(self, root: Path, record: dict) -> Path:
        path = root / "PVG-KIX.json"
        path.write_text(
            json.dumps(
                {
                    "route": "PVG-KIX",
                    "dates": {self.DATE.isoformat(): record},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def _refresh(self, root: Path, source) -> dict:
        from price_calendar import update_calendar
        from request_cache import reset_for_tests

        reset_for_tests(root / "request_cache")

        with (
            patch("price_calendar._query_dates", return_value=[self.DATE]),
            patch("price_calendar._shanghai_now", return_value=self.NOW),
            patch(
                "price_calendar.shanghai_today",
                return_value=self.FIXED_TODAY,
            ),
            patch("request_cache.DEFAULT_CACHE_DIR", root / "request_cache"),
        ):
            return update_calendar(
                "PVG-KIX",
                "PVG",
                "KIX",
                self.DATE.isoformat(),
                source,
                data_dir=root,
                sleep_seconds=0,
                round_id="round-freshness",
            )

    def test_failed_refresh_preserves_old_price_only_as_history(self):
        from analyzer import build_price_hint_from_calendar
        from notifier import _email_price_calendar_body
        from price_calendar import analyze_date_savings, calendar_rows

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            previous_success = (self.NOW - timedelta(hours=7)).isoformat()
            self._write_calendar(
                root,
                _legacy_success(500, updated_at=previous_success),
            )

            calendar = self._refresh(root, _FailingSource())

        record = calendar["dates"][self.DATE.isoformat()]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["min_price"], 500)
        self.assertEqual(record["last_success_at"], previous_success)
        self.assertEqual(record["last_attempt_at"], self.NOW.isoformat())
        self.assertEqual(record["error_type"], "PermissionError")
        self.assertEqual(record["round_id"], "round-freshness")
        with patch(
            "price_calendar.shanghai_today",
            return_value=self.FIXED_TODAY,
        ):
            savings = analyze_date_savings(
                calendar,
                (self.DATE + timedelta(days=1)).isoformat(),
                900,
                threshold=1,
            )
            rows = calendar_rows(calendar, self.DATE.isoformat())
        self.assertEqual(savings, [])
        self.assertEqual(rows[0]["status"], "failed")
        self.assertFalse(rows[0]["eligible_for_recommendation"])
        self.assertFalse(build_price_hint_from_calendar(calendar)["has_data"])
        body = _email_price_calendar_body(
            {
                "price_calendar": {
                    "rows": rows,
                    "scope": "oneway",
                    "savings": [],
                    "weekday_pattern": {"data_insufficient": True},
                },
                "recommended_plans": [],
            }
        )
        self.assertIn("采集失败(PermissionError)", body)

    def test_empty_refresh_records_attempt_and_excludes_old_price(self):
        from notifier import _email_price_calendar_body
        from price_calendar import analyze_weekday_pattern, calendar_price_on_date
        from price_calendar import calendar_rows

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            previous_success = (self.NOW - timedelta(hours=7)).isoformat()
            self._write_calendar(
                root,
                _legacy_success(480, updated_at=previous_success),
            )

            calendar = self._refresh(root, _EmptySource())

        record = calendar["dates"][self.DATE.isoformat()]
        self.assertEqual(record["status"], "empty")
        self.assertEqual(record["min_price"], 480)
        self.assertEqual(record["last_success_at"], previous_success)
        self.assertIsNone(record["error_type"])
        self.assertIsNone(calendar_price_on_date(calendar, self.DATE.isoformat()))
        with patch(
            "price_calendar.shanghai_today",
            return_value=self.FIXED_TODAY,
        ):
            weekday_pattern = analyze_weekday_pattern(calendar, min_samples=1)
            rows = calendar_rows(calendar, self.DATE.isoformat())
        self.assertEqual(weekday_pattern, {"data_insufficient": True})
        body = _email_price_calendar_body(
            {
                "price_calendar": {
                    "rows": rows,
                    "scope": "oneway",
                    "savings": [],
                    "weekday_pattern": {"data_insufficient": True},
                },
                "recommended_plans": [],
            }
        )
        self.assertIn("本次无报价", body)

    def test_expired_success_loads_as_stale_and_renders_gray_history(self):
        from notifier import _email_price_calendar_body
        from price_calendar import analyze_date_savings, calendar_rows, load_calendar

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._write_calendar(
                root,
                {
                    "status": "success",
                    "min_price": 520,
                    "last_attempt_at": (self.NOW - timedelta(hours=8)).isoformat(),
                    "last_success_at": (self.NOW - timedelta(hours=8)).isoformat(),
                    "error_type": None,
                    "stale_after": (self.NOW - timedelta(hours=2)).isoformat(),
                    "round_id": "old-round",
                },
            )
            with (
                patch("price_calendar._shanghai_now", return_value=self.NOW),
                patch(
                    "price_calendar.shanghai_today",
                    return_value=self.FIXED_TODAY,
                ),
            ):
                calendar = load_calendar("PVG-KIX", root)
                rows = calendar_rows(calendar, self.DATE.isoformat())
                savings = analyze_date_savings(
                    calendar,
                    (self.DATE + timedelta(days=1)).isoformat(),
                    900,
                    threshold=1,
                )

        self.assertEqual(calendar["dates"][self.DATE.isoformat()]["status"], "stale")
        self.assertFalse(rows[0]["eligible_for_recommendation"])
        self.assertEqual(savings, [])
        body = _email_price_calendar_body(
            {
                "price_calendar": {
                    "rows": rows,
                    "scope": "oneway",
                    "savings": [],
                    "weekday_pattern": {"data_insufficient": True},
                },
                "recommended_plans": [],
            }
        )
        self.assertIn("历史参考", body)
        self.assertIn("color:#888", body)

    def test_relative_failed_calendar_semantics_follow_injected_today(self):
        from price_calendar import calendar_rows

        normalized = []
        expected = {
            "status": "failed",
            "relative_days": 2,
            "eligible_for_recommendation": False,
            "historical_reference": True,
            "selected": True,
            "scope": "oneway",
        }
        for fixed_today in (
            date(2026, 8, 29),
            date(2026, 8, 30),
            date(2026, 8, 31),
        ):
            target = fixed_today + timedelta(days=2)
            attempt_at = datetime.combine(
                fixed_today, time(9), tzinfo=SHANGHAI_TZ
            ).isoformat()
            calendar = {
                "route": "PVG-KIX",
                "dates": {
                    target.isoformat(): {
                        "status": "failed",
                        "min_price": 500,
                        "last_attempt_at": attempt_at,
                        "last_success_at": attempt_at,
                        "error_type": "PermissionError",
                        "round_id": "round-relative-clock",
                    }
                },
            }

            with self.subTest(fixed_today=fixed_today.isoformat()):
                with patch(
                    "price_calendar.shanghai_today",
                    return_value=fixed_today,
                ):
                    rows = calendar_rows(calendar, target.isoformat())

                self.assertEqual(len(rows), 1)
                row = rows[0]
                normalized_result = (
                    {
                        "status": row["status"],
                        "relative_days": (
                            date.fromisoformat(row["date"]) - fixed_today
                        ).days,
                        "eligible_for_recommendation": row[
                            "eligible_for_recommendation"
                        ],
                        "historical_reference": row["historical_reference"],
                        "selected": row["selected"],
                        "scope": row["scope"],
                    }
                )
                self.assertEqual(normalized_result, expected)
                normalized.append(normalized_result)

        self.assertEqual(normalized, [expected] * 3)

    def test_corrupt_calendar_raises_instead_of_becoming_empty(self):
        from atomic_json_store import JsonStoreReadError
        from price_calendar import load_calendar

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "PVG-KIX.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(JsonStoreReadError):
                load_calendar("PVG-KIX", root)

    def test_atomic_save_failure_leaves_original_bytes_unchanged(self):
        from price_calendar import save_calendar

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            path = self._write_calendar(
                root,
                _legacy_success(500, updated_at=self.NOW.isoformat()),
            )
            before = path.read_bytes()
            with (
                patch(
                    "atomic_json_store.os.replace",
                    side_effect=OSError("simulated replace failure"),
                ),
                self.assertRaises(OSError),
            ):
                save_calendar(
                    "PVG-KIX",
                    {"route": "PVG-KIX", "dates": {}},
                    root,
                )
            after = path.read_bytes()

        self.assertEqual(before, after)

    def test_shanghai_day_controls_past_filter_and_flex_date_boundary(self):
        from collection_plan import _flex_dates
        from price_calendar import _query_dates, calendar_rows

        shanghai_day = date(2026, 8, 28)
        with (
            patch("price_calendar.shanghai_today", return_value=shanghai_day),
            patch("collection_plan.shanghai_today", return_value=shanghai_day),
        ):
            query_dates = _query_dates("2026-08-29")
            rows = calendar_rows(
                {
                    "route": "PVG-KIX",
                    "dates": {
                        "2026-08-27": {"min_price": 400},
                        "2026-08-28": {"min_price": 500},
                    },
                },
                "2026-08-28",
            )
            flex_dates = _flex_dates("2026-08-29", 3)

        self.assertNotIn(date(2026, 8, 27), query_dates)
        self.assertEqual([row["date"] for row in rows], ["2026-08-28"])
        self.assertNotIn("2026-08-27", flex_dates)

    def test_production_date_calculations_do_not_use_host_local_today(self):
        project_root = Path(__file__).resolve().parent
        forbidden = re.compile(
            r"(?:date\.today\(\)|datetime\.now\(\)\.date\(\))"
        )
        offenders = []
        for path in project_root.rglob("*.py"):
            relative = path.relative_to(project_root)
            if path.name.startswith("test_") or "tests" in relative.parts:
                continue
            if forbidden.search(path.read_text(encoding="utf-8")):
                offenders.append(relative.as_posix())
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
