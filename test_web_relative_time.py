import unittest
from datetime import datetime, timedelta, timezone

from project_time import SHANGHAI_TZ
from web_form import _relative_time_label


class RelativeTimeLabelTest(unittest.TestCase):
    def test_equivalent_aware_offsets_render_the_same_elapsed_time(self):
        now = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)

        for value in (
            "2026-08-28T02:00:00Z",
            "2026-08-28T10:00:00+08:00",
            "2026-08-28T02:00:00+00:00",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    _relative_time_label(value, now=now),
                    "1小时前",
                )

    def test_legacy_naive_timestamp_is_interpreted_as_shanghai_time(self):
        self.assertEqual(
            _relative_time_label(
                "2026-08-28T10:00:00",
                now=datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc),
            ),
            "1小时前",
        )

    def test_label_is_independent_of_aware_now_timezone(self):
        value = "2026-08-28T09:00:00+08:00"
        utc_now = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
        shanghai_now = datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ)

        self.assertEqual(_relative_time_label(value, now=utc_now), "1小时前")
        self.assertEqual(
            _relative_time_label(value, now=shanghai_now),
            _relative_time_label(value, now=utc_now),
        )

    def test_naive_now_is_interpreted_as_utc(self):
        self.assertEqual(
            _relative_time_label(
                "2026-08-28T09:00:00+08:00",
                now=datetime(2026, 8, 28, 2, 0),
            ),
            "1小时前",
        )

    def test_elapsed_time_crosses_shanghai_midnight_correctly(self):
        self.assertEqual(
            _relative_time_label(
                "2026-08-27T23:45:00",
                now=datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc),
            ),
            "45分钟前",
        )

    def test_slightly_future_timestamp_still_renders_just_now(self):
        now = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)

        self.assertEqual(
            _relative_time_label(
                (now + timedelta(minutes=3)).isoformat(),
                now=now,
            ),
            "刚刚",
        )

    def test_minute_and_hour_boundaries_are_preserved(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        cases = (
            (timedelta(minutes=59), "59分钟前"),
            (timedelta(minutes=60), "1小时前"),
            (timedelta(hours=23), "23小时前"),
            (timedelta(hours=24), "1天前"),
        )

        for elapsed, expected in cases:
            with self.subTest(elapsed=elapsed):
                self.assertEqual(
                    _relative_time_label((now - elapsed).isoformat(), now=now),
                    expected,
                )

    def test_empty_and_invalid_values_keep_existing_behavior(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(_relative_time_label("", now=now), "")
        self.assertEqual(_relative_time_label(None, now=now), "")
        self.assertEqual(
            _relative_time_label("not-a-timestamp", now=now),
            "not-a-timestamp",
        )


if __name__ == "__main__":
    unittest.main()
