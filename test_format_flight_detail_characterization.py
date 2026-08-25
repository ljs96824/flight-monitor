import copy
import html
import unittest

from notifier import format_flight_detail


def _segment(
    flight_no,
    dep_airport,
    arr_airport,
    dep_time,
    arr_time,
    *,
    airline="东方航空",
    equipment="320",
):
    return {
        "flight_no": flight_no,
        "dep_airport": dep_airport,
        "arr_airport": arr_airport,
        "dep_time": dep_time,
        "arr_time": arr_time,
        "airline": airline,
        "equipment": equipment,
    }


def _priced_flight(segments, *, price, stops, source="juhe", **extra):
    return {
        "segments": segments,
        "price": price,
        "stops": stops,
        "source": source,
        "collected_at": "2026-08-25T10:00:00+08:00",
        **extra,
    }


class FormatFlightDetailCharacterizationTest(unittest.TestCase):
    def test_direct_domestic_and_international_outputs_are_exact(self):
        domestic = _priced_flight(
            [
                _segment(
                    "MU5101",
                    "SHA",
                    "PEK",
                    "2026-09-01 08:05",
                    "2026-09-01 10:20",
                )
            ],
            price=880,
            stops=0,
        )
        international = _priced_flight(
            [
                _segment(
                    "MM080",
                    "PVG",
                    "KIX",
                    "2026-10-01 12:05",
                    "2026-10-01 15:10",
                    airline="乐桃航空",
                    equipment="32S",
                )
            ],
            price=2885,
            stops=0,
        )

        self.assertEqual(
            format_flight_detail(domestic, "2026-09-01", "去程"),
            "去程:MU5101｜东方航空 | 虹桥(SHA) 08:05(上海当地) → "
            "首都(PEK) 10:20(北京当地) | 直飞｜空客A320 | "
            "¥880 (来源:聚合数据（国内报价）, 采集于10:00)",
        )
        self.assertEqual(
            format_flight_detail(international, "2026-10-01", "去程"),
            "去程:MM080｜乐桃航空｜廉航 | 浦东(PVG) 12:05(上海当地) → "
            "关西(KIX) 15:10(大阪当地) | 直飞｜空客A320 | "
            "¥2,885 (来源:聚合数据（国内报价）, 采集于10:00)",
        )

    def test_one_stop_and_multi_segment_outputs_are_exact(self):
        one_stop = _priced_flight(
            [
                _segment("MU5001", "PVG", "ICN", "2026-10-01 08:00", "2026-10-01 10:30"),
                _segment("MU5002", "ICN", "KIX", "2026-10-01 12:00", "2026-10-01 13:30"),
            ],
            price=3200,
            stops=1,
            layovers=[{"airport": "ICN", "wait_minutes": 90}],
            duration_min=330,
        )
        multi = _priced_flight(
            [
                _segment("CA1", "PVG", "PEK", "2026-10-01 06:00", "2026-10-01 08:00"),
                _segment("CA2", "PEK", "ICN", "2026-10-01 09:00", "2026-10-01 11:00"),
                _segment("CA3", "ICN", "KIX", "2026-10-01 12:00", "2026-10-01 13:30"),
            ],
            price=3600,
            stops=2,
            duration_min=450,
        )

        self.assertEqual(
            format_flight_detail(one_stop, "2026-10-01", "去程"),
            "去程:东方航空 | 浦东(PVG) 08:00(上海当地) → 经仁川(ICN)中转 → "
            "关西(KIX) 13:30(大阪当地) | 中转1次 ICN | "
            "¥3,200 (来源:聚合数据（国内报价）, 采集于10:00)",
        )
        self.assertEqual(
            format_flight_detail(multi, "2026-10-01", "去程"),
            "去程:中国国际航空 | 浦东(PVG) 06:00(上海当地) → 经首都(PEK)中转 → "
            "关西(KIX) 13:30(大阪当地) | 中转2次 PEK | "
            "¥3,600 (来源:聚合数据（国内报价）, 采集于10:00)",
        )

    def test_missing_times_aircraft_and_fare_rules_are_characterized(self):
        flight = {
            "segments": [
                {
                    "flight_no": "MU?",
                    "dep_airport": "PVG",
                    "arr_airport": "KIX",
                    "airline": "测试航司",
                }
            ],
            "price": 0,
            "stops": "bad",
        }
        before = copy.deepcopy(flight)

        rendered = format_flight_detail(flight, "2026-10-01", "去程")

        self.assertEqual(
            rendered,
            "去程:MU?｜东方航空 | 浦东(PVG) 待确认(上海当地) → "
            "关西(KIX) 待确认(大阪当地) | 直飞 | 暂无报价",
        )
        self.assertNotIn("机型", rendered)
        self.assertNotIn("行李", rendered)
        self.assertNotIn("退改", rendered)
        self.assertEqual(flight, before)

    def test_empty_segments_combo_fallback_none_and_active_signature_are_exact(self):
        combo_only = {
            "flight_combo": "MU225+JL891",
            "price": 1000,
            "source": "juhe",
            "collected_at": "2026-08-25T10:00:00+08:00",
        }
        self.assertEqual(
            format_flight_detail(combo_only, "2026-10-01", "去程"),
            "去程:MU225+JL891｜东方航空 | 机场待确认 待确认(当地当地) → "
            "机场待确认 待确认(当地当地) | 直飞 | "
            "¥1,000 (来源:聚合数据（国内报价）, 采集于10:00)",
        )
        self.assertEqual(format_flight_detail({}, "2026-10-01", "去程"), "去程:航班信息待确认")
        self.assertEqual(format_flight_detail(None), "航班:航班信息待确认")
        with self.assertRaisesRegex(TypeError, "takes from 1 to 3 positional arguments but 5 were given"):
            format_flight_detail(combo_only, "2026-10-01", None, {}, {})

    def test_special_characters_stay_plain_text_until_html_rendering(self):
        flight = _priced_flight(
            [
                _segment(
                    "<MU&1>",
                    "PVG",
                    "KIX",
                    "2026-10-01 08:00",
                    "2026-10-01 10:00",
                    airline="A&B <测试>",
                    equipment="",
                )
            ],
            price=1234,
            stops=0,
            source="x&<y>",
            fare_rules={},
            has_baggage_info=False,
        )

        rendered = format_flight_detail(flight, "2026-10-01", "去程")

        self.assertEqual(
            rendered,
            "去程:<MU&1>｜A&B <测试> | 浦东(PVG) 08:00(上海当地) → "
            "关西(KIX) 10:00(大阪当地) | 直飞 | "
            "¥1,234 (来源:x&<y>, 采集于10:00)",
        )
        self.assertNotIn("&lt;", rendered)
        self.assertIn("&lt;MU&amp;1&gt;", html.escape(rendered))

    def test_date_argument_is_not_rendered_and_input_is_not_mutated(self):
        flight = _priced_flight(
            [_segment("MU5101", "SHA", "PEK", "08:05", "10:20")],
            price=880,
            stops=0,
        )
        before = copy.deepcopy(flight)

        first = format_flight_detail(flight, "2026-09-01", "去程")
        second = format_flight_detail(flight, "2030-01-01", "去程")

        self.assertIsInstance(first, str)
        self.assertEqual(first, second)
        self.assertEqual(flight, before)


if __name__ == "__main__":
    unittest.main()
