import copy
import unittest

from analyzer import verify_fare_rules


def _complete_live_fare_flight():
    return {
        "flight_no": "JL891",
        "route_type": "international",
        "cabin_class": "economy",
        "stops": 0,
        "airlines": ["JL"],
        "fare_rules": {
            "source": "Duffel live",
            "baggage": {
                "included": True,
                "checked_kg": 23,
                "checked_pieces": 1,
                "level": "verified",
            },
            "refund": {
                "allowed": True,
                "fee": "CNY300",
                "label": "可退改",
                "level": "高",
                "note": "可退",
            },
            "change": {"allowed": True},
        },
    }


class VerifyFareRulesCharacterizationTest(unittest.TestCase):
    def assert_result_shape(self, result, expected):
        self.assertEqual(result, expected)
        self.assertEqual(list(result), ["level", "label", "matches", "issues"])
        self.assertIsInstance(result["level"], str)
        self.assertIsInstance(result["label"], str)
        self.assertIsInstance(result["matches"], list)
        self.assertIsInstance(result["issues"], list)

    def test_complete_live_rules_lock_full_return_and_no_mutation(self):
        flight = _complete_live_fare_flight()
        constraints = {"baggage": "required", "refund_flexibility": "required"}
        original_flight = copy.deepcopy(flight)
        original_constraints = copy.deepcopy(constraints)

        result = verify_fare_rules(flight, constraints)

        self.assert_result_shape(
            result,
            {
                "level": "full",
                "label": "票规完全匹配",
                "matches": ["含托运行李 23kg/1件", "可退改", "可退改"],
                "issues": [],
            },
        )
        self.assertEqual(flight, original_flight)
        self.assertEqual(constraints, original_constraints)
        self.assertEqual(flight["fare_rules"]["source"], "Duffel live")
        self.assertEqual(flight["fare_rules"]["baggage"]["level"], "verified")
        self.assertEqual(flight["fare_rules"]["refund"]["level"], "高")

    def test_baggage_explicit_and_missing_states_are_distinct(self):
        included = {
            "route_type": "international",
            "fare_rules": {
                "source": "Duffel live",
                "baggage": {"included": True},
            },
        }
        result = verify_fare_rules(included, {"baggage": "required"})
        self.assert_result_shape(
            result,
            {
                "level": "full",
                "label": "票规完全匹配",
                "matches": ["含托运行李 标准kg/1件"],
                "issues": [],
            },
        )

        rule_missing = {
            "route_type": "international",
            "fare_rules": {"source": "Duffel live"},
        }
        result = verify_fare_rules(rule_missing, {"baggage": "required"})
        self.assert_result_shape(
            result,
            {
                "level": "mismatch",
                "label": "票规需确认",
                "matches": [],
                "issues": ["托运行李规则待确认，购买前请核实"],
            },
        )

        info_missing = {"route_type": "international"}
        result = verify_fare_rules(info_missing, {"baggage": "required"})
        self.assert_result_shape(
            result,
            {
                "level": "mismatch",
                "label": "票规需确认",
                "matches": [],
                "issues": ["托运行李信息未确认，购买前请核实"],
            },
        )

    def test_refund_explicit_negative_and_missing_rules_lock_issue_order(self):
        explicit = {
            "route_type": "international",
            "fare_rules": {
                "source": "Duffel live",
                "refund": {
                    "allowed": False,
                    "level": "低",
                    "label": "退改严格",
                    "note": "该票不可退",
                },
                "change": {"allowed": False},
            },
        }
        result = verify_fare_rules(explicit, {"refund_flexibility": "required"})
        self.assert_result_shape(
            result,
            {
                "level": "mismatch",
                "label": "票规需确认",
                "matches": [],
                "issues": ["该票不可退", "该票不可退"],
            },
        )

        missing = {
            "route_type": "international",
            "fare_rules": {"source": "Duffel live"},
        }
        result = verify_fare_rules(missing, {"refund_flexibility": "required"})
        self.assert_result_shape(
            result,
            {
                "level": "mismatch",
                "label": "票规需确认",
                "matches": [],
                "issues": ["退票规则未确认，购买前请核实", "改签规则未确认"],
            },
        )

    def test_domestic_source_conflict_is_overwritten_by_inferred_lcc_rules(self):
        flight = {
            "flight_no": "KN5978",
            "flight_combo": "KN5978",
            "route_type": "domestic",
            "airline": "KN",
            "cabin_code": "Z",
            "stops": 0,
            "fare_rules": {
                "source": "Duffel live",
                "baggage": {"included": True, "checked_kg": 30},
                "refund": {"allowed": True, "level": "高"},
            },
        }

        result = verify_fare_rules(
            flight,
            {"baggage": "required", "refund_flexibility": "required"},
        )

        self.assert_result_shape(
            result,
            {
                "level": "mismatch",
                "label": "票规需确认",
                "matches": ["国内标准规则推断，具体条款以支付页为准"],
                "issues": [
                    "不含免费托运行李，需额外购买",
                    "特价舱，退改费高或不可退，适合确定行程",
                    "特价舱，退改费高或不可退，适合确定行程",
                ],
            },
        )
        fare_rules = flight["fare_rules"]
        self.assertEqual(fare_rules["source"], "国内标准规则推断")
        self.assertEqual(
            fare_rules["source_note"],
            "国内标准规则推断，实付和具体条款以支付页为准",
        )
        self.assertEqual(fare_rules["baggage"]["level"], "需加购")
        self.assertFalse(fare_rules["baggage"]["included"])
        self.assertEqual(fare_rules["refund"]["level"], "低")
        self.assertFalse(fare_rules["change"]["allowed"])

    def test_system_inferred_full_service_rules_lock_evidence_fields(self):
        flight = {
            "flight_no": "CA1234",
            "route_type": "domestic",
            "airline": "CA",
            "cabin_code": "Y",
            "stops": 0,
        }

        result = verify_fare_rules(
            flight,
            {"baggage": "required", "refund_flexibility": "required"},
        )

        self.assert_result_shape(
            result,
            {
                "level": "full",
                "label": "票规完全匹配",
                "matches": [
                    "含托运行李 20kg/1件",
                    "退改友好",
                    "退改友好",
                    "国内标准规则推断，具体条款以支付页为准",
                ],
                "issues": [],
            },
        )
        fare_rules = flight["fare_rules"]
        self.assertEqual(fare_rules["source"], "国内标准规则推断")
        self.assertEqual(fare_rules["baggage"]["level"], "标准")
        self.assertEqual(fare_rules["refund"]["level"], "高")
        self.assertTrue(fare_rules["change"]["allowed"])

    def test_payment_page_pending_international_inference_wording(self):
        flight = {
            "route_type": "international",
            "fare_rules": {
                "source": "国内标准规则推断",
                "source_note": "待支付页确认",
                "baggage": {"included": None},
                "refund": {
                    "level": "中",
                    "label": "退改适中",
                    "note": "以支付页为准",
                },
                "change": {"allowed": True},
            },
            "stops": 0,
        }
        original = copy.deepcopy(flight)

        result = verify_fare_rules(
            flight,
            {"baggage": "required", "refund_flexibility": "preferred"},
        )

        self.assert_result_shape(
            result,
            {
                "level": "partial",
                "label": "票规部分匹配",
                "matches": [
                    "退改适中",
                    "标准规则推断(国际线)，具体条款以支付页为准",
                ],
                "issues": ["托运行李规则待确认，购买前请核实"],
            },
        )
        self.assertEqual(flight, original)

    def test_mixed_cabin_passenger_details_do_not_change_current_verification(self):
        baseline = _complete_live_fare_flight()
        mixed = {
            **copy.deepcopy(baseline),
            "cabin_arrangement": "mixed",
            "cabin_allocation": {
                "business": {"adult": 2, "child": 0, "elderly": 0, "infant": 0},
                "economy": {"adult": 0, "child": 1, "elderly": 2, "infant": 0},
            },
            "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
        }
        constraints = {"baggage": "required", "refund_flexibility": "required"}
        expected = verify_fare_rules(copy.deepcopy(baseline), constraints)
        original = copy.deepcopy(mixed)

        result = verify_fare_rules(mixed, constraints)

        self.assert_result_shape(result, expected)
        self.assertEqual(mixed, original)

    def test_basic_cabin_cross_airline_and_invalid_inputs(self):
        flight = {
            **_complete_live_fare_flight(),
            "cabin_class": "basic_economy",
            "stops": 1,
            "airlines": ["MU", "JL"],
        }
        result = verify_fare_rules(flight, {})
        self.assert_result_shape(
            result,
            {
                "level": "mismatch",
                "label": "票规需确认",
                "matches": [],
                "issues": [
                    "基础经济舱/轻选舱，可能不含行李、不可选座、不可退改",
                    "跨航司中转，可能为非联程票，需确认",
                ],
            },
        )

        self.assert_result_shape(
            verify_fare_rules(None, None),
            {"level": "full", "label": "票规完全匹配", "matches": [], "issues": []},
        )
        with self.assertRaisesRegex(AttributeError, "get"):
            verify_fare_rules("not-a-flight", {})
        with self.assertRaisesRegex(AttributeError, "get"):
            verify_fare_rules({}, "not-constraints")


if __name__ == "__main__":
    unittest.main()
