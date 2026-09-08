"""Reserve horizon admission, independent of host time and production inputs."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


TARGET = date(2026, 10, 1)


def policy():
    return {
        "kind": "purchased_packs", "packs": [{"added": 1100}],
        "reserve": {"kind": "workload_p90", "target_date": TARGET.isoformat(),
                    "minimum_daily_p90": 10, "safety_multiplier": 1.2,
                    "manual_live_buffer": 30, "research_batch_calls": 30},
    }


def usage(today):
    days = [(today - timedelta(days=i)).isoformat() for i in range(7, 0, -1)]
    return {"version": 2, "dates": {day: {"juhe": 4} for day in days},
            "entries": [{"day": day, "counts": {"juhe": 4},
                         "workload_class": "scheduled_user_monitor"} for day in days]}


def metrics(today, config=None):
    from quota_policy import metrics as calculate
    return calculate(config or policy(), {"cumulative": {"juhe": 100}}, "juhe",
                     usage_payload=usage(today), as_of=today)


def quota(today, **changes):
    values = metrics(today)
    result = {
        "complete": True, "quota_ledger_healthy": True,
        "expected_days_remaining": 60, "worst_case_days_remaining": 40,
        "remaining_after_research": 995, "quota_remaining": 1000,
        "monitoring_reserve": values["reserve"],
        "research_available": values["research_available"],
        "research_batch_calls": 30, "scheduled_anomaly": False,
        "reserve_kind": "workload_p90", "reserve_details": values["reserve_details"],
    }
    result.update(changes)
    return result


def backup():
    return {"checks": {name: True for name in (
        "backup_restore_verified", "off_disk_copy_verified",
        "different_device_verified", "off_disk_copy_fresh")}}


def migrations():
    return {"timestamp_ready": True, "lineage_ready": True, "old_data_readable": True}


def evaluate(values):
    from research_cohort import evaluate_research_hard_gates
    return evaluate_research_hard_gates(backup_evidence=backup(),
                                       quota_simulation=values, migration_status=migrations())


def assert_horizon_refused(result):
    assert not result["ready"] and result["checks"].get("reserve_horizon") is False, "reserve_horizon_not_blocked"


def state():
    from research_cohort import _new_cohort_state
    return {"revision": 1, "research_cohort_v2": _new_cohort_state()}


def prepare(original, today, values, root, user_dates=None):
    import basket_collect
    with ExitStack() as stack:
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(patch("basket_collect._load_active_subscriptions_for_research", return_value=[]))
        stack.enter_context(patch("basket_collect.active_user_monitor_dates", return_value=user_dates or set()))
        clock = stack.enter_context(patch("research_cohort.datetime"))
        clock.now.return_value = datetime.combine(today, datetime.min.time())
        stack.enter_context(patch("basket_collect._simulate_runtime_quota", return_value=values))
        stack.enter_context(patch("basket_collect.inspect_research_migrations", return_value=migrations()))
        stack.enter_context(patch("basket_collect.load_backup_evidence", return_value=backup()))
        return basket_collect._prepare_research_basket(
            original, today=today, state_path=root / "basket_state.json",
            db_path=root / "observations.sqlite3", usage_path=root / "api_usage.json",
            settings={"research_cohort_v2_gates": {}}, source_builder=Mock(),
            quota_guard_notifier=Mock(return_value=False))


class ReserveHorizonTest(unittest.TestCase):
    def test_runtime_quota_transmits_configured_kind_without_changing_simulation(self):
        import basket_collect
        from quota_policy import metrics as calculate
        from research_cohort import apply_research_quota_guard, simulate_research_quota
        fixed = {"kind": "purchased_packs", "packs": [{"added": 1100}], "reserve": 30}
        for config, drop_details, expected_kind in (
            (policy(), False, "workload_p90"), (policy(), True, "workload_p90"),
            (fixed, False, None), (1100, False, None),
        ):
            with self.subTest(kind=expected_kind, drop_details=drop_details), ExitStack() as stack:
                settings = {"source_quota_budget": {"juhe": config},
                            "research_cohort_v2_gates": {"scheduled_subscription_runs_per_day": 3,
                                                         "other_non_subscription_calls_per_day": 0}}
                before = deepcopy(settings)
                values = calculate(config, {"cumulative": {"juhe": 100}}, "juhe",
                                   usage_payload=usage(TARGET), as_of=TARGET)
                if drop_details:
                    values.pop("reserve_details")
                basket_keys, subscription_keys = {("juhe", "basket")}, {("juhe", "user")}
                stack.enter_context(patch("basket_collect.build_collection_plan", side_effect=[
                    Mock(request_keys=basket_keys), Mock(request_keys=subscription_keys)]))
                stack.enter_context(patch("basket_collect.usage_ledger_health", return_value={
                    "healthy": True, "usage": usage(TARGET)}))
                stack.enter_context(patch("basket_collect.usage_snapshot", return_value={
                    "cumulative": {"juhe": 100}}))
                stack.enter_context(patch("basket_collect.quota_metrics", return_value=values))
                simulated = stack.enter_context(patch("basket_collect.simulate_research_quota",
                                                       wraps=simulate_research_quota))
                actual = basket_collect._simulate_runtime_quota(
                    research_requests=[{}], subscriptions=[], settings=settings,
                    source_builder=Mock(), usage_path="unused-offline-ledger", today=TARGET)
                self.assertEqual(actual.get("reserve_kind"), expected_kind)
                self.assertEqual(settings, before)
                expected_inputs = dict(basket_keys=basket_keys, subscription_keys=subscription_keys,
                    scheduled_subscription_runs_per_day=3, other_non_subscription_calls_per_day=0,
                    quota_remaining=1000, retries_per_request=1, monitoring_reserve=values["reserve"])
                simulated.assert_called_once_with(**expected_inputs)
                for key, value in simulate_research_quota(**expected_inputs).items():
                    self.assertEqual(actual[key], value, key)
                self.assertEqual(actual["reserve_details"], values.get("reserve_details", {}))
                gate = evaluate(actual)
                self.assertEqual(gate["ready"], not drop_details)
                guarded = state()
                unchanged = deepcopy(guarded)
                notify = Mock()
                guard = apply_research_quota_guard(guarded, actual, notifier=notify,
                                                   now="2026-10-01T12:00:00+08:00")
                self.assertEqual(bool(guard.get("admission_blocked")), drop_details)
                if drop_details:
                    self.assertEqual(gate["missing"], ["reserve_horizon"])
                    self.assertEqual(guard["reason_codes"], ["reserve_horizon_metadata_invalid"])
                self.assertEqual(guarded, unchanged)
                notify.assert_not_called()

    def test_numeric_boundary_and_explicit_metadata(self):
        for offset, days, coverage, reserve, status in (
            (-1, 1, 1, 42, "active"), (0, 0, 1, 42, "active"),
            (1, 0, 0, 30, "expired"), (7, 0, 0, 30, "expired"),
        ):
            with self.subTest(offset=offset):
                values = metrics(TARGET + timedelta(days=offset))
                details = values["reserve_details"]
                self.assertEqual(details.get("horizon_status"), status)
                self.assertEqual(details["horizon_days"], -offset)
                self.assertEqual(details["days_remaining"], days)
                self.assertEqual(details["reserve_coverage_days"], coverage)
                self.assertEqual((details["observed_raw_p90"], details["effective_scheduled_p90"]), (4, 10))
                self.assertTrue(details["minimum_floor_applied"])
                self.assertEqual(values["reserve"], reserve)
                self.assertEqual(values["research_available"], 1000 - reserve)

    def test_target_day_retains_monitoring_reserve_and_t_zero_request(self):
        from research_cohort import prepare_research_requests
        self.assertGreater(metrics(TARGET)["reserve"], 30)
        schedule = prepare_research_requests(state(), today=TARGET, user_monitor_dates=set())
        self.assertTrue(any(row["sample_role"] == "trajectory_anchor"
                            and row["depart_date"] == TARGET.isoformat() for row in schedule.requests))
        self.assertTrue(evaluate(quota(TARGET))["ready"])

    def test_expired_next_batch_is_false_without_falsifying_balance(self):
        values = metrics(TARGET + timedelta(days=1))
        self.assertEqual(values["research_available"], 970)
        self.assertFalse(values["reserve_details"]["next_batch_can_start"])

    def test_expired_gate_rejects_when_all_other_gates_pass(self):
        result = evaluate(quota(TARGET + timedelta(days=1)))
        assert_horizon_refused(result)
        self.assertEqual(result["missing"], ["reserve_horizon"])
        self.assertTrue(all(ok for name, ok in result["checks"].items() if name != "reserve_horizon"))
        self.assertIn("reserve_horizon_expired", result["reasons"]["reserve_horizon"])

    def test_missing_or_inconsistent_metadata_is_rejected(self):
        valid = quota(TARGET)
        bad_details = [None, {}, {"reserve_kind": "workload_p90"}]
        for field, value in (("horizon_status", "active-but-unknown"),
                             ("as_of", "invalid"), ("target_date", "invalid"),
                             ("horizon_days", 7), ("reserve_coverage_days", 0)):
            row = deepcopy(valid["reserve_details"])
            row[field] = value
            bad_details.append(row)
        expired = quota(TARGET + timedelta(days=1))["reserve_details"]
        expired["horizon_status"] = "active"
        bad_details.append(expired)
        missing_kind = deepcopy(valid["reserve_details"])
        missing_kind.pop("reserve_kind")
        bad_details.append(missing_kind)
        for details in bad_details:
            with self.subTest(details=details):
                result = evaluate({**valid, "reserve_details": details})
                self.assertFalse(result["ready"])
                self.assertIn("reserve_horizon_metadata_invalid", result["reasons"]["reserve_horizon"])

    def test_guard_blocks_expiry_without_permanent_disablement(self):
        from research_cohort import apply_research_quota_guard
        original = state()
        before = deepcopy(original)
        notify = Mock()
        result = apply_research_quota_guard(original, quota(TARGET + timedelta(days=1)),
                                           notifier=notify, now="2026-10-02T12:00:00+08:00")
        self.assertTrue(result.get("admission_blocked"))
        self.assertFalse(result["triggered"])
        self.assertIn("reserve_horizon_expired", result["reason_codes"])
        self.assertEqual(original, before)
        notify.assert_not_called()

    def test_readiness_uses_gate_reason_and_missing(self):
        from research_readiness import build_readiness_summary, render_readiness_summary
        result = evaluate(quota(TARGET + timedelta(days=1)))
        summary = build_readiness_summary(result)
        self.assertIn("reserve_horizon", summary["missing"])
        self.assertFalse(summary["ready"])
        output = render_readiness_summary(result)
        self.assertIn("reserve_horizon_expired", output)
        self.assertIn("待", output)

    def test_expired_preparation_discards_progress_and_requests(self):
        original = state()
        before = deepcopy(original)
        with tempfile.TemporaryDirectory() as tmp:
            result, requests, gate, _ = prepare(original, TARGET + timedelta(days=1),
                                                quota(TARGET + timedelta(days=1)), Path(tmp))
        self.assertFalse(gate["ready"])
        self.assertEqual(requests, [])
        self.assertEqual(result, before)
        self.assertEqual(original, before)

    def test_permanent_guard_preserves_only_guard_changes_not_planner_progress(self):
        for today in (TARGET, TARGET + timedelta(days=1)):
            with self.subTest(today=today), tempfile.TemporaryDirectory() as tmp:
                original = state()
                before = deepcopy(original)
                collision = (today + timedelta(days=original["research_cohort_v2"]["probes"]["probe_1"]["target_t"])).isoformat()
                result, requests, gate, values = prepare(original, today,
                    quota(today, manual_live_in_epoch=31), Path(tmp), {collision})
                self.assertFalse(gate["ready"])
                self.assertEqual(requests, [])
                self.assertTrue(values["guard_triggered"])
                cohort = result["research_cohort_v2"]
                self.assertFalse(cohort["runtime_enabled"])
                self.assertTrue(cohort["user_monitoring_enabled"])
                self.assertEqual(cohort["anchors"], before["research_cohort_v2"]["anchors"])
                self.assertEqual(cohort["probes"], before["research_cohort_v2"]["probes"])
                self.assertEqual(cohort["events"], before["research_cohort_v2"]["events"])
                self.assertEqual(original, before)

    def test_non_workload_policy_keeps_numeric_and_admission_behavior(self):
        from quota_policy import metrics as calculate
        values = calculate({"kind": "purchased_packs", "packs": [{"added": 1100}], "reserve": 30},
                           {"cumulative": {"juhe": 100}}, "juhe", as_of=TARGET + timedelta(days=7))
        self.assertEqual(values, {"kind": "purchased_packs", "total_limit": 1100,
                                "used": 100, "remaining": 1000, "reserve": 30,
                                "research_available": 970})
        result = evaluate(quota(TARGET, reserve_kind="fixed", reserve_details={}))
        self.assertTrue(result["ready"])

    def test_invalid_config_target_fails_before_returning_numbers(self):
        for value in (None, "not-a-date"):
            config = policy()
            config["reserve"]["target_date"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                metrics(TARGET, config)

    def test_user_monitor_plan_is_not_blocked_by_research_horizon(self):
        from collection_plan import CollectionPlan
        import quota_policy
        plan = CollectionPlan(subscription_count=1)
        source = Mock(name="offline-source")
        source.name = "juhe"
        plan.add_request(source, "SHA", "PEK", (TARGET + timedelta(days=2)).isoformat(), persist=False)
        with patch("quota_policy._as_date", return_value=TARGET + timedelta(days=1)), redirect_stdout(io.StringIO()):
            self.assertFalse(quota_policy.metrics(policy(), {"cumulative": {"juhe": 100}}, "juhe")
                             ["reserve_details"]["next_batch_can_start"])
            plan.log_summary(quota_budgets={"juhe": policy()},
                             usage_snapshot={"cumulative": {"juhe": 100}})
        self.assertEqual(plan._quota_protected_keys, set())
        source.fetch.assert_not_called()

    def test_removing_period_check_is_killed_by_same_admission_assertion(self):
        values = quota(TARGET + timedelta(days=1))
        assert_horizon_refused(evaluate(values))
        with patch("workload_reserve.evaluate_reserve_horizon", return_value={
            "eligible": True, "status": "active", "reason_code": None,
        }):
            old_admission = evaluate(values)
        self.assertTrue(old_admission["ready"])
        with self.assertRaisesRegex(AssertionError, "reserve_horizon_not_blocked"):
            assert_horizon_refused(old_admission)

    def test_guard_rejects_invalid_metadata_without_disabling_or_notifying(self):
        from research_cohort import apply_research_quota_guard
        original = state()
        before = deepcopy(original)
        notify = Mock()
        result = apply_research_quota_guard(original, quota(TARGET, reserve_details={}),
                                           notifier=notify, now="2026-10-01T12:00:00+08:00")
        self.assertTrue(result["admission_blocked"])
        self.assertEqual(result["reason_codes"], ["reserve_horizon_metadata_invalid"])
        self.assertFalse(result["triggered"])
        self.assertEqual(original, before)
        notify.assert_not_called()

    def test_sequential_days_and_new_target_do_not_reenable_disabled_state(self):
        from research_cohort import apply_research_quota_guard, research_runtime_enabled
        original = state()
        with tempfile.TemporaryDirectory() as tmp:
            for offset in (-1, 0, 1, 7):
                today = TARGET + timedelta(days=offset)
                before = deepcopy(original)
                result, requests, gate, _ = prepare(original, today, quota(today), Path(tmp))
                self.assertEqual(gate["ready"], offset <= 0)
                if offset > 0:
                    self.assertEqual(result, before)
                    self.assertEqual(requests, [])
                original = result
        apply_research_quota_guard(original, quota(TARGET, manual_live_in_epoch=31),
                                   now="2026-10-01T12:00:00+08:00")
        disabled = deepcopy(original)
        moved = policy()
        moved["reserve"]["target_date"] = (TARGET + timedelta(days=10)).isoformat()
        values = quota(TARGET + timedelta(days=1))
        values["reserve_details"] = metrics(TARGET + timedelta(days=1), moved)["reserve_details"]
        self.assertTrue(evaluate(values)["checks"]["reserve_horizon"])
        apply_research_quota_guard(original, values, now="2026-10-02T12:00:00+08:00")
        self.assertFalse(research_runtime_enabled(original, True))
        self.assertEqual(original, disabled)

    def test_real_basket_entry_never_executes_or_saves_progress_when_expired(self):
        import basket_collect
        for extra in ({}, {"manual_live_in_epoch": 31}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                today = TARGET + timedelta(days=1)
                original = state()
                before = deepcopy(original)
                path = root / "basket_state.json"
                path.write_text(json.dumps(original), encoding="utf-8")
                before_bytes = path.read_bytes()
                settings = {"research_basket_enabled": True, "research_basket_strategy": "cohort_v2"}
                stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(patch("basket_collect.load_collection_settings", return_value=settings))
                stack.enter_context(patch("basket_collect.acquire_collection_singleflight", return_value=Mock(acquired=True)))
                stack.enter_context(patch("basket_collect.load_or_create_state", return_value=original))
                stack.enter_context(patch("basket_collect._load_active_subscriptions_for_research", return_value=[]))
                stack.enter_context(patch("basket_collect._simulate_runtime_quota", return_value=quota(today, **extra)))
                stack.enter_context(patch("basket_collect.inspect_research_migrations", return_value=migrations()))
                stack.enter_context(patch("basket_collect.load_backup_evidence", return_value=backup()))
                clock = stack.enter_context(patch("research_cohort.datetime"))
                clock.now.return_value = datetime(2026, 10, 2, 12)
                for name in ("start_round_log_archive", "end_round_log_archive"):
                    stack.enter_context(patch("basket_collect." + name))
                side_effects = {name: stack.enter_context(patch("basket_collect." + name)) for name in (
                    "build_collection_plan", "start_request_cache_round", "set_current_round",
                    "apply_research_round_outcomes", "_default_quota_guard_notifier")}
                def persist(_path, value, _revision):
                    _path.write_text(json.dumps(value), encoding="utf-8")
                    return value
                saved = stack.enter_context(patch("basket_collect._persist_state", side_effect=persist))
                notify = Mock(return_value=False)
                result = basket_collect.run_basket(today=today, now=datetime(2026, 10, 2, 12),
                    state_path=path, db_path=root / "observations.sqlite3", usage_path=root / "api_usage.json",
                    source_builder=Mock(), aggregator_factory=Mock(), quota_guard_notifier=notify)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["reason"], "research_hard_gate")
                self.assertEqual((result["queues"], result["written"]), (0, 0))
                self.assertTrue(result["user_monitoring_enabled"])
                for mock in side_effects.values():
                    mock.assert_not_called()
                stored = json.loads(path.read_text(encoding="utf-8"))["research_cohort_v2"]
                for key in ("anchors", "probes", "events"):
                    self.assertEqual(stored[key], before["research_cohort_v2"][key])
                self.assertEqual(original, before)
                self.assertEqual(saved.call_count, int(bool(extra)))
                self.assertEqual(notify.call_count, int(bool(extra)))
                if not extra:
                    self.assertEqual(path.read_bytes(), before_bytes)
                else:
                    self.assertFalse(stored["runtime_enabled"])
                self.assertFalse((root / "observations.sqlite3").exists())
                self.assertFalse((root / "api_usage.json").exists())


if __name__ == "__main__":
    unittest.main()
