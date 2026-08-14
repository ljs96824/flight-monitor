"""离线捕获表单 POST 到规范化订阅的兼容性基线。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import web_form  # noqa: E402


def _base_form(**overrides) -> dict:
    form = {
        "origin_select": "PVG",
        "destination": "KIX",
        "route_type": "international",
        "round_trip": "true",
        "depart_date": "2026-12-01",
        "return_date": "2026-12-06",
        "date_flexibility": "1",
        "return_date_flexibility": "1",
        "price_strategy": "explicit",
        "max_budget": "8000",
        "target_price": "6000",
        "max_budget_scope": "per_person",
        "target_price_scope": "per_person",
        "transfer_policy": "reasonable",
        "baggage": "required",
        "primary_goal": "buy_timing",
        "travel_scenario": ["personal"],
        "notification_method": "page_only",
        "notification_frequency": "important_only",
        "lcc_policy": "any",
    }
    form.update(overrides)
    return form


SCENARIOS = {
    "family_elderly_tourism": _base_form(
        monitor_mode="precise",
        travel_scenario=["tourism", "family", "elderly"],
        adult_count="2",
        child_count="1",
        elderly_count="1",
        infant_count="0",
        companion_constraints=["direct_preferred", "no_redeye", "need_baggage"],
        elderly_condition="limited_walk_transfer",
        child_type="school_age",
        time_preference="daytime",
        refund_flexibility="preferred",
    ),
    "parallel_scenarios_elderly_child": _base_form(
        monitor_mode="precise",
        travel_scenario=["tourism", "family", "elderly"],
        adult_count="2",
        child_count="1",
        elderly_count="1",
        infant_count="0",
        companion_constraints=["direct_preferred", "no_redeye", "need_baggage"],
        elderly_condition="mobility_limited",
        child_type="school_age",
        time_preference="daytime",
        refund_flexibility="preferred",
    ),
    "business_meeting": _base_form(
        monitor_mode="precise",
        destination="PEK",
        route_type="domestic",
        depart_date="2026-11-18",
        return_date="2026-11-20",
        travel_scenario=["business"],
        adult_count="1",
        child_count="0",
        elderly_count="0",
        infant_count="0",
        trip_natures=["business", "meeting"],
        meeting_start="10:30",
        meeting_end="17:00",
        invoice_context="true",
        invoice_needed="true",
        cabin_policy="economy_only",
        reimburse_per_person="5000",
    ),
    "same_day_round_trip": _base_form(
        monitor_mode="quick",
        destination="北京",
        route_type="domestic",
        round_trip="false",
        depart_date="2026-11-26",
        return_date="",
        same_day_round_trip="true",
        business_start="10:30",
        business_end="17:00",
        meeting_location="大兴区",
        meeting_importance="important",
        passenger_count="3",
        travel_scenario=["business"],
        transfer_policy="direct_only",
    ),
    "solo_minimal": _base_form(
        monitor_mode="quick",
        destination="大阪",
        round_trip="false",
        return_date="",
        date_flexibility="0",
        return_date_flexibility="0",
        price_strategy="auto_judge",
        max_budget="",
        target_price="",
        passenger_count="1",
        travel_scenario=["personal"],
        baggage="unknown",
    ),
    "team_trip": _base_form(
        monitor_mode="precise",
        destination="PEK",
        route_type="domestic",
        depart_date="2026-12-10",
        return_date="2026-12-12",
        travel_scenario=["business"],
        adult_count="8",
        child_count="0",
        elderly_count="0",
        infant_count="0",
        trip_natures=["business", "team_building"],
        team_passenger_count="8",
        team_date_flexibility="flexible",
        same_flight_required="true",
        cabin_arrangement="mixed",
        cabin_policy="level_based",
        user_level="director",
        business_seats="2",
        economy_seats="6",
        reimburse_per_person="4500",
        invoice_context="true",
        invoice_needed="true",
    ),
    "same_day_meeting_complete": _base_form(
        monitor_mode="precise",
        destination="北京",
        route_type="domestic",
        round_trip="false",
        depart_date="2026-12-18",
        return_date="",
        same_day_round_trip="true",
        business_start="10:30",
        business_end="17:00",
        meeting_location="大兴区",
        meeting_importance="important",
        buffer_hours="1.5",
        transport_mode="taxi",
        user_transport_min="25",
        redundancy_min="15",
        adult_count="2",
        child_count="0",
        elderly_count="0",
        infant_count="0",
        travel_scenario=["business"],
        transfer_policy="direct_only",
        notification_method="email",
        notification_email="same-day@example.com",
    ),
    "email_only_notification": _base_form(
        monitor_mode="precise",
        notification_method="email",
        notification_email="email-only@example.com",
        notification_frequency="daily_digest",
        notification_frequency_rule="daily_digest",
    ),
    "both_notification": _base_form(
        monitor_mode="precise",
        notification_method="both",
        notification_email="both@example.com",
        notification_frequency="price_change",
        notification_frequency_rule="price_change",
        price_change_threshold="down_200",
        digest_time="08:30",
    ),
    "directional_time_windows": _base_form(
        monitor_mode="precise",
        ux2_concept_form="true",
        ux2_time_touched="true",
        time_preference="unlimited",
        allow_redeye="false",
        arrival_preference="any",
        shared_departure_window_start="08:00",
        shared_departure_window_end="12:00",
        shared_arrival_window_start="10:00",
        shared_arrival_window_end="15:00",
        outbound_departure_window_start="06:30",
        outbound_departure_window_end="08:30",
        outbound_arrival_window_start="09:00",
        outbound_arrival_window_end="11:00",
        return_departure_window_start="18:00",
        return_departure_window_end="21:00",
        return_arrival_window_start="20:00",
        return_arrival_window_end="23:00",
    ),
}


def capture() -> dict:
    original_path = web_form.SUBSCRIPTIONS_PATH
    web_form.app.config.update(TESTING=True)
    records = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        web_form.SUBSCRIPTIONS_PATH = Path(tmpdir) / "subscriptions.json"
        try:
            with patch.object(web_form, "start_background_collection"):
                client = web_form.app.test_client()
                for name, form_input in SCENARIOS.items():
                    response = client.post("/subscribe", data=form_input)
                    if response.status_code != 302:
                        raise RuntimeError(
                            f"场景{name} POST失败: status={response.status_code} "
                            f"body={response.get_data(as_text=True)[:300]}"
                        )
                    saved = json.loads(web_form.SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))[-1]
                    records[name] = {
                        "form_input": form_input,
                        "normalized_subscription": main.normalize_subscription(saved),
                    }
        finally:
            web_form.SUBSCRIPTIONS_PATH = original_path
    return {
        "version": "form_normalization_baseline_v1",
        "transport": "POST /subscribe",
        "scenarios": records,
    }


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=str(ROOT / "tests" / "fixtures" / "form_normalization_baseline_v1.json"),
    )
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = capture()
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"[表单兼容基线] 场景={len(payload['scenarios'])} 输出={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
