import copy
import unittest

from analyzer import determine_push_type


def _available_flight(*, price=700, grade="A", age=10, stops=1):
    return {
        "price": price,
        "execution_grade": grade,
        "stops": stops,
        "availability": {
            "status": "likely_available",
            "age_minutes": age,
        },
    }


def _nearby_calendar(selected=1000, nearby=900):
    return {
        "price_calendar": {
            "rows": [
                {"date": "2026-10-01", "selected": True, "min_price": selected},
                {"date": "2026-10-02", "selected": False, "min_price": nearby},
            ]
        }
    }


class DeterminePushTypeCharacterizationTest(unittest.TestCase):
    def assert_result_shape(self, result, expected):
        self.assertEqual(result, expected)
        self.assertEqual(
            list(result),
            ["type", "reasons", "price_change", "percentile", "historical_30_price"],
        )
        self.assertIsInstance(result["type"], str)
        self.assertIsInstance(result["reasons"], list)
        self.assertTrue(result["price_change"] is None or isinstance(result["price_change"], dict))
        self.assertTrue(result["percentile"] is None or isinstance(result["percentile"], int))
        self.assertTrue(
            result["historical_30_price"] is None
            or isinstance(result["historical_30_price"], float)
        )
        if result["price_change"] is not None:
            self.assertEqual(
                list(result["price_change"]),
                ["last", "current", "diff", "direction"],
            )

    def test_abnormal_low_locks_price_roles_history_reasons_and_no_mutation(self):
        analysis = {
            "decision_prices": {
                "display_price": 700,
                "budget_compare_price": 750,
                "transaction_price": 600,
                "verify_price": 800,
            },
            "all_flights": [_available_flight()],
        }
        history = [1300, {"price": 900}, ("2026-08-20", 1100), {"total": 1000}, 1200]
        originals = copy.deepcopy((analysis, history))

        result = determine_push_type(
            999,
            target_price=800,
            max_budget=650,
            price_history=history,
            days_to_dept=7,
            last_push_price=800,
            analysis_result=analysis,
        )

        self.assert_result_shape(
            result,
            {
                "type": "异常低价",
                "reasons": [
                    "搜索参考价进入你的理想入手区间（你的设置）",
                    "较上次提醒：下降¥100（上次同口径提醒）",
                    "当前搜索价低于所有相似采集记录，处于近期低位（n=5）",
                    "距出发7天，低价继续变化的风险上升",
                ],
                "price_change": {
                    "last": 800.0,
                    "current": 700.0,
                    "diff": -100.0,
                    "direction": "down",
                },
                "percentile": 0,
                "historical_30_price": 1000.0,
            },
        )
        self.assertEqual((analysis, history), originals)

    def test_worth_verifying_locks_transaction_mismatch_copy(self):
        analysis = {
            "decision_prices": {
                "display_price": 800,
                "budget_compare_price": 850,
                "transaction_price": 1000,
                "verify_price": 900,
            }
        }
        result = determine_push_type(
            999,
            target_price=700,
            days_to_dept=30,
            analysis_result=analysis,
        )
        self.assert_result_shape(
            result,
            {
                "type": "值得验证",
                "reasons": [
                    "搜索参考价达标，但预估实付价高于验证购买价（你的设置）",
                    "搜索参考价距离理想入手价还差¥150（你的设置）",
                    "符合你设置的直飞条件",
                ],
                "price_change": None,
                "percentile": None,
                "historical_30_price": None,
            },
        )

    def test_target_nearby_and_same_day_branches(self):
        target_result = determine_push_type(
            900,
            target_price=1000,
            max_budget=800,
            days_to_dept=30,
            analysis_result={},
        )
        self.assert_result_shape(
            target_result,
            {
                "type": "进入低价区间",
                "reasons": [
                    "搜索参考价进入你的理想入手区间（你的设置）",
                    "符合你设置的直飞条件",
                ],
                "price_change": None,
                "percentile": None,
                "historical_30_price": None,
            },
        )

        nearby_result = determine_push_type(
            1000,
            days_to_dept=30,
            analysis_result=_nearby_calendar(),
        )
        self.assert_result_shape(
            nearby_result,
            {
                "type": "前后日期更便宜",
                "reasons": ["符合你设置的直飞条件"],
                "price_change": None,
                "percentile": None,
                "historical_30_price": None,
            },
        )

        same_day_result = determine_push_type(
            1000,
            days_to_dept=30,
            analysis_result={
                "all_flights": [
                    _available_flight(price=800, grade="A", stops=1),
                    _available_flight(price=900, grade="C", stops=1),
                ]
            },
        )
        self.assert_result_shape(
            same_day_result,
            {
                "type": "同日更优方案",
                "reasons": [],
                "price_change": None,
                "percentile": None,
                "historical_30_price": None,
            },
        )

    def test_price_down_rise_risk_and_flat_outputs(self):
        down = determine_push_type(900, last_push_price=1000, days_to_dept=30)
        self.assert_result_shape(
            down,
            {
                "type": "价格下降",
                "reasons": [
                    "较上次提醒：下降¥100（上次同口径提醒）",
                    "符合你设置的直飞条件",
                ],
                "price_change": {
                    "last": 1000.0,
                    "current": 900.0,
                    "diff": -100.0,
                    "direction": "down",
                },
                "percentile": None,
                "historical_30_price": None,
            },
        )

        rise = determine_push_type(1000, last_push_price=900, days_to_dept=14)
        self.assert_result_shape(
            rise,
            {
                "type": "涨价风险",
                "reasons": [
                    "较上次提醒：上涨¥100（上次同口径提醒）",
                    "符合你设置的直飞条件",
                    "距出发14天，低价继续变化的风险上升",
                ],
                "price_change": {
                    "last": 900.0,
                    "current": 1000.0,
                    "diff": 100.0,
                    "direction": "up",
                },
                "percentile": None,
                "historical_30_price": None,
            },
        )

        flat = determine_push_type(1000, last_push_price=1000, days_to_dept=30)
        self.assert_result_shape(
            flat,
            {
                "type": "同日更优方案",
                "reasons": [
                    "与上次提醒价格持平（上次同口径提醒）",
                    "符合你设置的直飞条件",
                ],
                "price_change": {
                    "last": 1000.0,
                    "current": 1000.0,
                    "diff": 0.0,
                    "direction": "flat",
                },
                "percentile": None,
                "historical_30_price": None,
            },
        )

    def test_time_conflict_missing_price_and_stale_price_priority(self):
        conflict = determine_push_type(
            None,
            analysis_result={
                "round_trip_analysis": {
                    "same_day_time_conflict": True,
                    "top_combinations": [],
                }
            },
        )
        self.assert_result_shape(
            conflict,
            {
                "type": "时间冲突提示",
                "reasons": ["符合你设置的直飞条件"],
                "price_change": None,
                "percentile": None,
                "historical_30_price": None,
            },
        )

        failed = determine_push_type(
            None,
            analysis_result={"source_errors": {"juhe": "timeout"}, "data_incomplete": True},
        )
        self.assert_result_shape(
            failed,
            {
                "type": "价格已失效",
                "reasons": ["符合你设置的直飞条件"],
                "price_change": None,
                "percentile": None,
                "historical_30_price": None,
            },
        )

        stale_analysis = {
            "decision_prices": {
                "display_price": 700,
                "transaction_price": 600,
                "verify_price": 800,
            },
            "all_flights": [_available_flight(age=121)],
        }
        stale = determine_push_type(
            999,
            target_price=800,
            price_history=[900, 1000, 1100, 1200, 1300],
            analysis_result=stale_analysis,
        )
        self.assertEqual(stale["type"], "价格已失效")
        self.assertEqual(stale["percentile"], 0)
        self.assertEqual(stale["historical_30_price"], 1000.0)

    def test_over_budget_waiting_and_data_incomplete_have_no_distinct_type(self):
        analysis = {
            "data_incomplete": True,
            "collection_failed": True,
            "source_errors": {"juhe": "timeout"},
        }
        with_budget = determine_push_type(
            2000,
            max_budget=1000,
            days_to_dept=30,
            analysis_result=analysis,
        )
        without_budget = determine_push_type(
            2000,
            max_budget=None,
            days_to_dept=30,
            analysis_result=analysis,
        )
        expected = {
            "type": "同日更优方案",
            "reasons": ["符合你设置的直飞条件"],
            "price_change": None,
            "percentile": None,
            "historical_30_price": None,
        }
        self.assert_result_shape(with_budget, expected)
        self.assertEqual(with_budget, without_budget)

    def test_multi_trigger_priority_follows_active_elif_order(self):
        all_flights = [
            _available_flight(price=700, grade="A", stops=1),
            _available_flight(price=800, grade="C", stops=1),
        ]
        nearby = _nearby_calendar()

        abnormal_analysis = {
            **nearby,
            "decision_prices": {
                "display_price": 700,
                "transaction_price": 600,
                "verify_price": 800,
            },
            "all_flights": all_flights,
        }
        self.assertEqual(
            determine_push_type(
                999,
                target_price=800,
                price_history=[900, 1000, 1100, 1200, 1300],
                days_to_dept=1,
                last_push_price=900,
                analysis_result=abnormal_analysis,
            )["type"],
            "异常低价",
        )

        worth_analysis = {
            **nearby,
            "decision_prices": {
                "display_price": 800,
                "transaction_price": 1000,
                "verify_price": 900,
            },
            "all_flights": all_flights,
        }
        self.assertEqual(
            determine_push_type(
                999,
                target_price=900,
                days_to_dept=1,
                last_push_price=1000,
                analysis_result=worth_analysis,
            )["type"],
            "值得验证",
        )

        target_analysis = {**nearby, "all_flights": all_flights}
        self.assertEqual(
            determine_push_type(
                800,
                target_price=900,
                days_to_dept=1,
                last_push_price=1000,
                analysis_result=target_analysis,
            )["type"],
            "进入低价区间",
        )
        self.assertEqual(
            determine_push_type(
                1000,
                days_to_dept=1,
                last_push_price=1100,
                analysis_result=target_analysis,
            )["type"],
            "前后日期更便宜",
        )
        self.assertEqual(
            determine_push_type(
                1000,
                days_to_dept=1,
                last_push_price=1100,
                analysis_result={"all_flights": all_flights},
            )["type"],
            "同日更优方案",
        )
        self.assertEqual(
            determine_push_type(1000, days_to_dept=1, last_push_price=1100)["type"],
            "价格下降",
        )

    def test_history_n_gate_reason_limit_and_invalid_inputs(self):
        result = determine_push_type(
            1000,
            target_price=900,
            price_history=[900, 1000, 1100, 1200],
            days_to_dept=1,
            last_push_price=800,
            analysis_result={
                "all_flights": [
                    {
                        "stops": 0,
                        "fare_verification": {"matches": ["含托运行李 23kg/1件"]},
                    }
                ]
            },
        )
        self.assertEqual(len(result["reasons"]), 4)
        self.assertEqual(
            result["reasons"],
            [
                "搜索参考价距离理想入手价还差¥100（你的设置）",
                "较上次提醒：上涨¥200（上次同口径提醒）",
                "同条件样本不足（当前n=4），继续积累中，暂不给出价格位置判断（n=4）",
                "符合你设置的直飞条件",
            ],
        )

        with self.assertRaisesRegex(AttributeError, "get"):
            determine_push_type(1000, analysis_result="not-a-result")
        with self.assertRaises(TypeError):
            determine_push_type(1000, price_history=1)


if __name__ == "__main__":
    unittest.main()
