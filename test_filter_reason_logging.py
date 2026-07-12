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
    def test_logs_lowest_five_rejections_from_direct_and_transfer_pools(self):
        from analyzer import _log_low_price_filter_rejections

        transfers = [
            _flight(f"TR{index}", 100 + index, stops=1)
            for index in range(6)
        ]
        directs = [
            _flight("MU730" if index == 0 else f"DR{index}", 500 + index)
            for index in range(6)
        ]
        pool = transfers + directs
        excluded = [
            {**flight, "exclude_reason": "用户不接受红眼/过早航班"}
            for flight in pool
        ]

        with patch("analyzer.safe_log") as log:
            _log_low_price_filter_rejections(
                pool,
                excluded,
                {"red_eye": "reject"},
                round_id="round-direct-transfer-five",
            )

        messages = [call.args[0] for call in log.call_args_list]
        self.assertEqual(len(messages), 10)
        self.assertTrue(any("combo=MU730 " in message for message in messages))
        self.assertFalse(any("combo=TR5 " in message for message in messages))
        self.assertFalse(any("combo=DR5 " in message for message in messages))

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
