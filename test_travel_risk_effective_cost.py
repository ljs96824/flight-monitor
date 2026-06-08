import sys
import types
import unittest


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)


class TravelRiskEffectiveCostTest(unittest.TestCase):
    def test_punctuality_and_effective_cost_for_domestic_flight(self):
        from analyzer import calc_effective_cost, enrich_travel_risk_and_cost
        from domestic_fare_rules import standardize_domestic_fare_rules

        flight = {
            "flight_no": "KN5978",
            "flight_combo": "KN5978",
            "airline": "KN",
            "price": 527,
            "total_duration_min": 130,
            "route_type": "domestic",
            "data_source": "juhe",
            "departure_airport": "PVG",
            "arrival_airport": "PKX",
            "segments": [
                {
                    "flight_no": "KN5978",
                    "dep_airport": "PVG",
                    "arr_airport": "PKX",
                    "dep_time": "2026-06-10 08:15",
                    "arr_time": "2026-06-10 10:25",
                }
            ],
        }
        flight["fare_rules"] = standardize_domestic_fare_rules(flight)

        effective = calc_effective_cost(flight, {"baggage": "required"})
        enrich_travel_risk_and_cost(flight, {"baggage": "required"})

        self.assertEqual(effective["ticket_price"], 527)
        self.assertEqual(effective["transport_cost"], 310)
        self.assertEqual(effective["baggage_cost"], 100)
        self.assertGreater(effective["effective_cost"], 1000)
        self.assertEqual(flight["punctuality"]["level"], "中等")
        self.assertIn("PVG", " ".join(flight["punctuality"]["risk_factors"]))
        self.assertIn("浦东离市区远", " ".join(flight["logistics_notes"]))

    def test_notifier_plan_card_shows_punctuality_and_effective_cost(self):
        from notifier import _render_payload_plan_card

        flight = {
            "punctuality": {
                "level": "较高",
                "risk_factors": ["PVG为繁忙枢纽，高峰易流控"],
                "note": "准点率为估算，非实时",
            },
            "effective_cost": {
                "effective_cost": 910,
                "breakdown_note": "票价680+机场交通约130+时间成本约100",
            },
            "logistics_notes": ["浦东离市区60分钟，如赶时间可考虑虹桥航班"],
        }
        plan = {
            "label": "方案A",
            "tier": "首选推荐",
            "is_roundtrip": False,
            "price": 680,
            "estimated_price": 680,
            "main_flight": flight,
            "summary": "MU5101 东方航空",
            "baggage_line": "行李:已含20kg托运",
        }

        html = _render_payload_plan_card(plan)

        self.assertIn("准点率", html)
        self.assertIn("有效出行成本", html)
        self.assertIn("浦东离市区", html)


if __name__ == "__main__":
    unittest.main()
