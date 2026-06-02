import unittest

from analyzer import _matched_constraint_reasons


class TriggerReasonEncodingTest(unittest.TestCase):
    def test_direct_condition_reason_is_readable_chinese(self):
        reasons = _matched_constraint_reasons({"all_flights": [{"stops": 0}]})

        self.assertIn("符合你设置的直飞条件", reasons)
        self.assertFalse(any("绗" in reason or "浣犺" in reason for reason in reasons))

    def test_baggage_condition_reason_is_readable_chinese(self):
        reasons = _matched_constraint_reasons(
            {
                "all_flights": [
                    {
                        "stops": 0,
                        "fare_verification": {
                            "matches": ["含托运行李 23kg/1件"],
                        },
                    }
                ]
            }
        )

        self.assertIn("符合你设置的托运行李要求", reasons)
        self.assertFalse(any("鎵樿繍" in reason or "琛屾潕" in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
