import unittest
from unittest.mock import patch


def _flight(combo, price, *, stops=0):
    return {
        "flight_combo": combo,
        "price": price,
        "stops": stops,
        "departure_time": "2026-10-01 09:00",
        "arrival_time": "2026-10-01 12:00",
    }


class FilterReasonLoggingTest(unittest.TestCase):
    def test_only_logs_rejected_flights_in_pool_lowest_five(self):
        from analyzer import _log_low_price_filter_rejections

        pool = [_flight(f"MU{index}", 100 + index * 10) for index in range(7)]
        excluded = [
            {**pool[0], "stops": 1, "exclude_reason": "用户设置必须直飞"},
            {**pool[4], "exclude_reason": "超过最高可接受价格"},
            {**pool[5], "exclude_reason": "用户不接受红眼/过早航班"},
        ]

        with patch("analyzer.safe_log") as log:
            _log_low_price_filter_rejections(
                pool,
                excluded,
                {"max_budget": 130},
                round_id="round-low-five",
            )

        messages = [call.args[0] for call in log.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertIn("combo=MU0 拒因=direct_only 值=stops=1", messages[0])
        self.assertIn("combo=MU4 拒因=max_budget 值=price=140,max_budget=130", messages[1])
        self.assertFalse(any("MU5" in message for message in messages))

    def test_each_round_logs_at_most_ten_filter_details(self):
        from analyzer import _log_low_price_filter_rejections

        with patch("analyzer.safe_log") as log:
            for group in range(3):
                pool = [
                    _flight(f"G{group}F{index}", 100 + index)
                    for index in range(5)
                ]
                excluded = [
                    {**flight, "exclude_reason": "用户不接受红眼/过早航班"}
                    for flight in pool
                ]
                _log_low_price_filter_rejections(
                    pool,
                    excluded,
                    {"red_eye": "reject"},
                    round_id="round-cap",
                )

        messages = [call.args[0] for call in log.call_args_list]
        self.assertEqual(len(messages), 10)
        self.assertTrue(all(message.startswith("[过滤明细]") for message in messages))


if __name__ == "__main__":
    unittest.main()
