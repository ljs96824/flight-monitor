import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch


SCHEMA = """
CREATE TABLE observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at TEXT NOT NULL,
  round_id TEXT NOT NULL,
  route_type TEXT NOT NULL,
  origin_airport TEXT NOT NULL,
  dest_airport TEXT NOT NULL,
  depart_date TEXT NOT NULL,
  days_to_departure INTEGER NOT NULL,
  cabin_class TEXT NOT NULL,
  source TEXT NOT NULL,
  flight_combo TEXT NOT NULL,
  airline TEXT,
  stops INTEGER,
  duration_min INTEGER,
  price_cny REAL NOT NULL,
  method_version TEXT NOT NULL,
  UNIQUE(round_id, source, origin_airport, dest_airport,
         depart_date, flight_combo, cabin_class)
)
"""


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "observations.sqlite3"
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(SCHEMA)
            connection.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _insert(
        self,
        *,
        observed_day,
        source,
        combo,
        price,
        origin="PVG",
        dest="KIX",
        depart_date="2026-10-01",
        route_type="international",
        cabin="economy",
        round_id=None,
    ):
        days = (date.fromisoformat(depart_date) - date.fromisoformat(observed_day)).days
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO observations (
                    observed_at, round_id, route_type, origin_airport,
                    dest_airport, depart_date, days_to_departure,
                    cabin_class, source, flight_combo, airline, stops,
                    duration_min, price_cny, method_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{observed_day}T09:00:00",
                    round_id or f"r_{observed_day}_{combo}_{source}",
                    route_type,
                    origin,
                    dest,
                    depart_date,
                    days,
                    cabin,
                    source,
                    combo,
                    "测试航司",
                    0,
                    150,
                    float(price),
                    "v1",
                ),
            )
            connection.commit()

    def _insert_known_pairs(self, count=10):
        for index in range(count):
            combo = f"MU{100 + index}"
            self._insert(
                observed_day="2026-07-20",
                source="juhe",
                combo=combo,
                price=100,
            )
            self._insert(
                observed_day="2026-07-20",
                source="hasdata",
                combo=combo,
                price=100 + index,
            )

    def test_dual_source_agreement_exact_values_and_gate(self):
        from provenance import compute_dual_source_agreement

        self._insert_known_pairs(10)
        self._insert(
            observed_day="2026-07-20",
            source="hasdata",
            combo="SINGLE1",
            price=88,
        )

        result = compute_dual_source_agreement(
            self.db_path,
            route="上海-大阪",
            as_of_date=date(2026, 7, 23),
            window_days=30,
            min_pairs=10,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sample_n"], 10)
        self.assertEqual(result["median_abs_diff_pct"], 4.5)
        self.assertEqual(result["within_5pct_pct"], 60.0)
        self.assertEqual(result["window"], ["2026-06-24", "2026-07-23"])
        self.assertEqual(result["sources"], ["hasdata", "juhe"])

        insufficient = compute_dual_source_agreement(
            self.db_path,
            route="上海-大阪",
            as_of_date=date(2026, 7, 23),
            window_days=30,
            min_pairs=11,
        )
        self.assertEqual(insufficient["status"], "insufficient")
        self.assertEqual(insufficient["sample_n"], 10)
        self.assertIsNone(insufficient["median_abs_diff_pct"])
        self.assertIsNone(insufficient["within_5pct_pct"])
        self.assertEqual(insufficient["summary"], "样本不足(n=10)")

    def test_single_source_day_creates_no_pair(self):
        from provenance import compute_dual_source_agreement

        self._insert(
            observed_day="2026-07-20",
            source="hasdata",
            combo="MU225",
            price=100,
        )
        result = compute_dual_source_agreement(
            self.db_path,
            route="上海-大阪",
            as_of_date=date(2026, 7, 23),
            min_pairs=1,
        )

        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["sample_n"], 0)

    def test_price_signal_metadata_keeps_full_history_and_real_window(self):
        import notifier

        roundtrip_history = [
            {
                "date": f"2026-07-{day:02d}",
                "total": 2000 + day,
                "sources": ["juhe"],
            }
            for day in range(1, 13)
        ]
        signal_history = notifier._price_history_for_push(
            {},
            {"round_trip_analysis": {"history": roundtrip_history}},
            True,
        )
        metadata = notifier._price_signal_provenance_metadata(
            signal_history,
            {},
            {"round_trip_analysis": {"history": roundtrip_history}},
            True,
        )

        self.assertEqual(len(signal_history), 12)
        self.assertEqual(metadata["sample_n"], 12)
        self.assertEqual(metadata["window"], ["2026-07-01", "2026-07-12"])
        self.assertEqual(metadata["sources"], ["juhe"])

        one_way_history = [
            (datetime(2026, 6, day, tzinfo=timezone.utc).timestamp(), 1000 + day)
            for day in range(1, 21)
        ]
        one_way = notifier._price_signal_provenance_metadata(
            one_way_history,
            {"price_history": one_way_history},
            {},
            False,
        )
        self.assertEqual(one_way["sample_n"], 20)
        self.assertEqual(one_way["window"], ["2026-06-01", "2026-06-20"])

    def test_payload_has_all_five_provenance_families(self):
        from method_registry import METHOD_VERSIONS
        from provenance import attach_payload_provenance

        agreement = {
            "status": "ok",
            "sample_n": 12,
            "median_abs_diff_pct": 3.25,
            "within_5pct_pct": 75.0,
            "window": ["2026-06-24", "2026-07-23"],
            "sources": ["hasdata", "juhe"],
            "summary": "n=12,中位相对差3.25%,差≤5%占比75.00%",
        }
        payload = {
            "route": "上海 → 大阪",
            "depart_date": "2026-10-01",
            "return_date": "2026-10-06",
            "route_type": "international",
            "price_references": {
                "absolute_min": {"price": 1745, "sample_size": 9},
                "recent_min": {"price": 1750, "sample_size": 5},
                "current_min": {"price": 1771, "sample_size": 2},
            },
            "price_calendar": {
                "scope": "oneway",
                "rows": [
                    {
                        "date": "2026-10-01",
                        "min_price": 1771,
                        "selected": True,
                    }
                ],
                "weekday_pattern": {
                    "median_by_weekday": {"周四": 1771},
                    "iqr_by_weekday": {"周四": [1750, 1900]},
                    "sample_count_by_weekday": {"周四": 5},
                    "sample_count": 5,
                    "tip": "周四中位数参考",
                },
            },
            "price_signal": {
                "label": "中",
                "summary": "搜索参考价处于相似历史样本中位",
                "percentile": 50,
                "sample_n": 9,
                "sources": ["hasdata", "juhe"],
            },
            "tcurve": {
                "route": "上海-大阪",
                "coverage": {"t_min": 60, "t_max": 90},
                "degraded_excluded_count": 1,
                "points": [
                    {
                        "t": 70,
                        "n": 5,
                        "median": 1800,
                        "p25": 1700,
                        "p75": 1900,
                        "sufficient": True,
                    }
                ],
            },
        }
        context = {
            "window": ["2026-06-24", "2026-07-23"],
            "sources": ["hasdata", "juhe"],
            "degraded_excluded": 1,
            "dual_source_agreement": agreement,
        }

        enriched = attach_payload_provenance(
            payload,
            context=context,
            computed_at="2026-07-23T10:00:00+08:00",
        )
        statistics = enriched["provenance"]["statistics"]
        expected = {
            "reftier.absolute_min",
            "calendar.2026-10-01.min",
            "weekday.周四.median",
            "price_signal.history_position",
            "tcurve.T70.median",
        }
        self.assertTrue(expected.issubset(statistics))
        for stat_key in expected:
            entry = statistics[stat_key]
            self.assertEqual(entry["stat_key"], stat_key)
            self.assertIn("method_version", entry)
            self.assertIn("sample_n", entry)
            self.assertIn("window", entry)
            self.assertIn("sources", entry)
            self.assertIn("degraded_excluded", entry)
            self.assertIn("bucket", entry)
            self.assertIn("dual_source_agreement", entry)
            self.assertEqual(entry["computed_at"], "2026-07-23T10:00:00+08:00")
        self.assertEqual(enriched["versions"], dict(METHOD_VERSIONS))
        self.assertEqual(
            enriched["price_signal"]["provenance"]["stat_key"],
            "price_signal.history_position",
        )
        self.assertEqual(
            enriched["price_calendar"]["rows"][0]["provenance"]["stat_key"],
            "calendar.2026-10-01.min",
        )
        self.assertEqual(
            enriched["price_calendar"]["rows"][0]["provenance"]["sources"],
            [],
        )
        self.assertEqual(
            enriched["price_calendar"]["rows"][0]["provenance"]["sample_n"],
            0,
        )
        self.assertEqual(
            enriched["price_signal"]["provenance"]["sources"],
            ["hasdata", "juhe"],
        )
        self.assertEqual(
            enriched["price_references"]["absolute_min"]["provenance"]["sources"],
            [],
        )
        self.assertEqual(
            enriched["tcurve"]["points"][0]["provenance"]["method_version"],
            METHOD_VERSIONS["tcurve"],
        )

    def test_panel_calendar_latest_value_has_one_day_cell_sample(self):
        from provenance import build_panel_report_payload

        for observed_day, price in (("2026-07-20", 1200), ("2026-07-21", 1100)):
            self._insert(
                observed_day=observed_day,
                source="juhe",
                combo=f"MU{observed_day[-2:]}",
                price=price,
            )

        payload = build_panel_report_payload(
            self.db_path,
            route="上海-大阪",
            as_of_date=date(2026, 7, 23),
            min_pairs=10,
            min_tcurve_sample=1,
        )
        row = next(item for item in payload["price_calendar"]["rows"] if item["date"] == "2026-10-01")

        self.assertEqual(row["min_price"], 1100)
        self.assertEqual(row["provenance"]["sample_n"], 1)
        self.assertEqual(row["provenance"]["window"], ["2026-07-21", "2026-07-21"])
        self.assertEqual(row["provenance"]["sources"], ["juhe"])

    def test_roundtrip_context_keeps_directional_agreements_separate(self):
        from provenance import (
            attach_payload_provenance,
            build_route_provenance_context_from_info,
            format_dual_source_agreement,
        )

        for index in range(10):
            combo = f"MU{200 + index}"
            self._insert(observed_day="2026-07-20", source="juhe", combo=combo, price=100)
            self._insert(observed_day="2026-07-20", source="hasdata", combo=combo, price=102)
            return_combo = f"MU{300 + index}"
            self._insert(
                observed_day="2026-07-20",
                source="juhe",
                combo=return_combo,
                price=200,
                origin="KIX",
                dest="PVG",
                depart_date="2026-10-06",
            )
            self._insert(
                observed_day="2026-07-20",
                source="hasdata",
                combo=return_combo,
                price=220,
                origin="KIX",
                dest="PVG",
                depart_date="2026-10-06",
            )

        context = build_route_provenance_context_from_info(
            {
                "origin": "PVG",
                "destination": "KIX",
                "depart_date": "2026-10-01",
                "return_date": "2026-10-06",
                "round_trip": True,
            },
            db_path=self.db_path,
            as_of_date=date(2026, 7, 23),
            min_pairs=10,
        )
        self.assertEqual(context["agreements"]["outbound"]["median_abs_diff_pct"], 2.0)
        self.assertEqual(context["agreements"]["return"]["median_abs_diff_pct"], 10.0)
        self.assertIn("去程", format_dual_source_agreement(context["dual_source_agreement"]))
        self.assertIn("返程", format_dual_source_agreement(context["dual_source_agreement"]))

        payload = attach_payload_provenance(
            {
                "route": "上海 → 大阪",
                "depart_date": "2026-10-01",
                "return_date": "2026-10-06",
                "is_roundtrip": True,
                "price_references": {"current": {"price": 300, "sample_size": 1}},
                "tcurve": {
                    "points": [
                        {
                            "t": 70,
                            "n": 5,
                            "median": 100,
                            "provenance": {
                                "window": ["2026-07-20", "2026-07-20"],
                                "sources": ["juhe"],
                            },
                        }
                    ]
                },
            },
            context=context,
        )
        self.assertEqual(
            payload["price_references"]["current"]["provenance"]["dual_source_agreement"]["scope"],
            "roundtrip",
        )
        self.assertNotIn(
            "scope",
            payload["tcurve"]["points"][0]["provenance"]["dual_source_agreement"],
        )

    def test_render_reference_registration_exposes_missing_envelope(self):
        import notifier

        payload = {
            "provenance": {
                "statistics": {},
                "referenced_stat_keys": [],
            }
        }
        with patch.object(notifier, "safe_log") as log:
            note = notifier._mark_provenance_reference(
                payload,
                "price_signal.history_position",
            )

        self.assertEqual(note, {})
        self.assertEqual(
            payload["provenance"]["referenced_stat_keys"],
            ["price_signal.history_position"],
        )
        self.assertTrue(
            any("[依据缺失] stat=price_signal.history_position" in str(call.args[0]) for call in log.call_args_list)
        )

    def test_calendar_renderer_registers_only_the_values_it_uses(self):
        import notifier
        from provenance import attach_payload_provenance

        payload = attach_payload_provenance(
            {
                "route": "上海 → 大阪",
                "price_calendar": {
                    "scope": "oneway",
                    "rows": [
                        {
                            "date": "2026-10-01",
                            "min_price": 1000,
                            "sample_n": 3,
                            "observed_at": "2026-07-20",
                            "sources": ["juhe"],
                        }
                    ],
                },
            },
            context={"dual_source_agreement": {"status": "insufficient", "sample_n": 0}},
        )

        notifier._email_price_calendar_body(payload)

        self.assertEqual(
            payload["provenance"]["referenced_stat_keys"],
            ["calendar.2026-10-01.min"],
        )

    def test_rendered_history_copy_uses_envelope_window_not_sample_guess(self):
        import notifier

        payload = {
            "price_signal": {
                "summary": "搜索参考价处于近期低位（n=12·窗口=近12次同条件采集）",
            },
            "provenance": {
                "statistics": {
                    "price_signal.history_position": {
                        "stat_key": "price_signal.history_position",
                        "sample_n": 12,
                        "window": ["2026-07-01", "2026-07-12"],
                    }
                },
                "referenced_stat_keys": [],
            },
        }

        text = notifier._price_signal_summary_with_provenance(payload)

        self.assertIn("n=12·窗口=2026-07-01~2026-07-12", text)
        self.assertNotIn("近12次同条件采集", text)

    def test_epoch_history_uses_its_observation_day_for_window(self):
        from provenance import attach_payload_provenance

        observed_at = datetime(2026, 7, 20, 12, tzinfo=timezone.utc).timestamp()
        payload = attach_payload_provenance(
            {
                "route": "上海 → 大阪",
                "price_history": [(observed_at, 1200)],
                "price_references": {
                    "absolute_min": {"price": 1200, "sample_size": 1},
                },
            },
            context={"window": ["2026-06-24", "2026-07-23"]},
            computed_at="2026-07-23T10:00:00+08:00",
        )

        self.assertEqual(
            payload["provenance"]["statistics"]["reftier.absolute_min"]["window"],
            ["2026-07-20", "2026-07-20"],
        )

    def test_dict_price_history_is_safe_for_reference_metadata(self):
        import notifier
        from analyzer import calculate_price_references

        normalized = notifier._normalize_price_history_for_refs(
            [
                {"date": "2026-07-20", "price": 1200},
                {"date": "2026-07-21", "price": 1000},
            ]
        )
        references = calculate_price_references(
            1300,
            normalized,
            [],
            70,
            [{"price": 1300}],
        )

        self.assertEqual(references["absolute_min"]["price"], 1000)
        self.assertEqual(references["absolute_min"]["sample_size"], 2)
        self.assertEqual(references["current_min"]["sample_size"], 1)
        self.assertEqual(
            references["absolute_min"]["window"],
            ["2026-07-20", "2026-07-21"],
        )

    def test_roundtrip_reference_metadata_exposes_exact_sample_sizes(self):
        from analyzer import analyze_roundtrip_prices

        result = analyze_roundtrip_prices(
            [
                {"date": "2026-07-20", "outbound": 1000, "return": 900, "total": 1900},
                {"date": "2026-07-21", "outbound": 1100, "return": 900, "total": 2000},
            ],
            2000,
            1100,
            900,
            days_to_dept=70,
        )

        self.assertEqual(result["references"]["current"]["sample_size"], 1)
        self.assertEqual(result["references"]["absolute_min"]["sample_size"], 2)
        self.assertEqual(
            result["references"]["absolute_min"]["window"],
            ["2026-07-20", "2026-07-21"],
        )

    def test_notification_envelope_sources_match_the_winning_panel_value(self):
        from provenance import attach_payload_provenance

        context = {
            "price_cells": [
                {
                    "depart_date": "2026-10-01",
                    "observed_day": "2026-07-20",
                    "min_price": 1000,
                    "sources": ["juhe"],
                },
                {
                    "depart_date": "2026-10-01",
                    "observed_day": "2026-07-21",
                    "min_price": 1200,
                    "sources": ["hasdata"],
                },
            ],
            "dual_source_agreement": {"status": "insufficient", "sample_n": 0},
        }
        payload = attach_payload_provenance(
            {
                "route": "上海 → 大阪",
                "depart_date": "2026-10-01",
                "price_history": [
                    {"date": "2026-07-20", "price": 1000},
                    {"date": "2026-07-21", "price": 1200},
                ],
                "price_references": {
                    "absolute_min": {
                        "price": 1000,
                        "sample_size": 2,
                        "window": ["2026-07-20", "2026-07-21"],
                    }
                },
                "price_signal": {
                    "summary": "历史位置",
                    "percentile": 50,
                    "sample_n": 2,
                    "window": ["2026-07-20", "2026-07-21"],
                },
            },
            context=context,
        )

        self.assertEqual(
            payload["price_references"]["absolute_min"]["provenance"]["sources"],
            ["juhe"],
        )
        self.assertEqual(
            payload["price_signal"]["provenance"]["sources"],
            ["hasdata", "juhe"],
        )

    def test_method_registry_is_frozen_and_unregistered_stat_fails(self):
        from method_registry import EXPECTED_METHOD_KEYS, METHOD_VERSIONS
        from provenance import build_envelope, expected_search_sources

        expected = {
            "obs_store",
            "tcurve",
            "weekday",
            "reftier",
            "calendar",
            "price_signal",
            "dual_source_agreement",
            "provenance",
        }
        self.assertEqual(EXPECTED_METHOD_KEYS, expected)
        self.assertEqual(set(METHOD_VERSIONS), expected)
        with self.assertRaises(AttributeError):
            EXPECTED_METHOD_KEYS.add("new_method")
        self.assertEqual(expected_search_sources("domestic"), {"juhe"})
        self.assertEqual(
            expected_search_sources("international"),
            {"hasdata", "juhe"},
        )
        self.assertEqual(
            expected_search_sources("greater_china"),
            {"hasdata", "juhe"},
        )
        with self.assertRaises(KeyError):
            build_envelope(
                "unknown.metric",
                sample_n=1,
                window=["2026-07-01", "2026-07-01"],
                sources=["juhe"],
                degraded_excluded=0,
                bucket="测试",
            )

    def test_producers_do_not_hardcode_registered_versions(self):
        project = Path(__file__).resolve().parent
        checks = {
            "tcurve.py": "tcurve_v1",
            "price_calendar.py": "weekday_v2",
            "observations_store.py": 'METHOD_VERSION = "v1"',
        }
        for filename, forbidden in checks.items():
            with self.subTest(filename=filename):
                source = (project / filename).read_text(encoding="utf-8")
                self.assertNotIn(forbidden, source)

    def test_detail_marks_missing_evidence_and_renders_complete_section(self):
        import notifier
        from provenance import attach_payload_provenance

        missing_payload = {
            "route": "上海 → 大阪",
            "provenance": {
                "statistics": {},
                "referenced_stat_keys": ["price_signal.history_position"],
            },
        }
        with patch.object(notifier, "safe_log") as log:
            missing = notifier._detail_provenance_body(missing_payload)
        self.assertIn("依据缺失", missing)
        self.assertTrue(
            any("[依据缺失] stat=price_signal.history_position" in str(call.args[0]) for call in log.call_args_list)
        )

        complete = attach_payload_provenance(
            {
                "route": "上海 → 大阪",
                "price_signal": {
                    "label": "中",
                    "summary": "历史位置参考",
                    "percentile": 50,
                    "sample_n": 12,
                },
            },
            context={
                "window": ["2026-06-24", "2026-07-23"],
                "sources": ["hasdata", "juhe"],
                "degraded_excluded": 0,
                "dual_source_agreement": {
                    "status": "ok",
                    "sample_n": 12,
                    "median_abs_diff_pct": 3.0,
                    "within_5pct_pct": 75.0,
                    "window": ["2026-06-24", "2026-07-23"],
                    "sources": ["hasdata", "juhe"],
                    "summary": "n=12,中位相对差3.00%,差≤5%占比75.00%",
                },
            },
            computed_at="2026-07-23T10:00:00+08:00",
        )
        with patch.object(notifier, "safe_log") as log:
            notifier._price_signal_summary_with_provenance(complete)
            detail = notifier.render_detail_html(complete)
        self.assertIn("数据依据", detail)
        self.assertIn("price_signal.history_position", detail)
        self.assertFalse(any("[依据缺失]" in str(call.args[0]) for call in log.call_args_list))

    def test_email_source_section_discloses_agreement(self):
        import notifier

        body = notifier._email_source_body(
            {
                "route_type": "international",
                "source_stats": {},
                "dual_source_agreement": {
                    "status": "ok",
                    "sample_n": 10,
                    "median_abs_diff_pct": 4.5,
                    "within_5pct_pct": 60.0,
                    "window": ["2026-06-24", "2026-07-23"],
                    "summary": "n=10,中位相对差4.50%,差≤5%占比60.00%",
                },
            }
        )
        self.assertIn("双源历史一致度", body)
        self.assertIn("中位相对差4.50%", body)

    def test_engine_and_report_are_read_only(self):
        from provenance import compute_dual_source_agreement
        from provenance import build_panel_report_payload
        from scripts.provenance_report import generate_report

        self._insert_known_pairs(10)
        before = self.db_path.read_bytes()
        compute_dual_source_agreement(
            self.db_path,
            route="上海-大阪",
            as_of_date=date(2026, 7, 23),
            min_pairs=10,
        )
        report = generate_report(
            db_path=self.db_path,
            route="上海-大阪",
            as_of_date=date(2026, 7, 23),
            min_pairs=10,
            min_tcurve_sample=1,
        )
        payload = build_panel_report_payload(
            self.db_path,
            route="上海-大阪",
            as_of_date=date(2026, 7, 23),
            min_pairs=10,
            min_tcurve_sample=1,
        )

        self.assertEqual(self.db_path.read_bytes(), before)
        for heading in ("五档参考价", "低价日历", "周几统计", "价格信号历史比较", "T曲线", "双源一致度"):
            self.assertIn(heading, report)
        statistics = payload["provenance"]["statistics"]
        calendar_entry = statistics["calendar.2026-10-01.min"]
        self.assertEqual(calendar_entry["window"], ["2026-07-20", "2026-07-20"])
        self.assertEqual(calendar_entry["degraded_excluded"], 0)
        self.assertEqual(
            statistics["reftier.current_min"]["window"],
            ["2026-07-20", "2026-07-20"],
        )
        self.assertEqual(statistics["price_signal.history_position"]["degraded_excluded"], 0)

    def test_panel_minimum_statistics_name_only_the_winning_source(self):
        from provenance import build_panel_report_payload

        self._insert(
            observed_day="2026-07-20",
            source="juhe",
            combo="MU225",
            price=900,
        )
        self._insert(
            observed_day="2026-07-20",
            source="hasdata",
            combo="MU225",
            price=1200,
        )

        payload = build_panel_report_payload(
            self.db_path,
            route="上海-大阪",
            as_of_date=date(2026, 7, 23),
            min_pairs=10,
            min_tcurve_sample=1,
        )
        statistics = payload["provenance"]["statistics"]
        self.assertEqual(statistics["reftier.absolute_min"]["sources"], ["juhe"])
        self.assertEqual(statistics["calendar.2026-10-01.min"]["sources"], ["juhe"])

    def test_weekday_version_is_v2_without_value_change(self):
        from method_registry import METHOD_VERSIONS
        from price_calendar import analyze_weekday_pattern

        calendar = {
            "dates": {
                "2026-07-27": {"min_price": 100},
                "2026-08-03": {"min_price": 100},
                "2026-08-10": {"min_price": 1000},
                "2026-07-28": {"min_price": 200},
                "2026-08-04": {"min_price": 200},
                "2026-08-11": {"min_price": 200},
            }
        }
        result = analyze_weekday_pattern(calendar, min_samples=6)
        self.assertEqual(result["median_by_weekday"]["周一"], 100)
        self.assertEqual(result["iqr_by_weekday"]["周一"], [100, 550])
        self.assertEqual(result["method_version"], METHOD_VERSIONS["weekday"])


if __name__ == "__main__":
    unittest.main()
