"""R35: offline contracts for local, product-specific low-price evidence."""
from contextlib import ExitStack, closing, redirect_stdout
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import analyzer
import main
import notifier
import observation_time
import observations_store
import request_cache
import source_profiles
import storage
from sources import juhe_source
from tests import test_return_phase_processing as scenario
from tests.test_low_price_alert_state import run_analysis


OBS = "low_price_alert_observations_v1"
CLAIMS = "low_price_alert_component_claims_v1"
EVAL = "low_price_alert_evaluations_v1"
T = "2026-09-30T08:00:00+00:00"
DAY = "2026-10-01"
PASSENGERS = {"adult": 1}
ALLOC = {"economy": {"adult": 1}, "business": {"adult": 1}}


def product(leg="outbound", cabin="economy", price=1200, event="E1"):
    f = scenario.flight(leg == "outbound", price)
    source = "juhe" if cabin == "economy" else "serpapi"
    u = dict(source=source, cabin_class=cabin, origin_airport=f["departure_airport"],
             dest_airport=f["arrival_airport"], depart_date=f["departure_date"], source_status="success",
             observed_at_utc=T, observation_round_id="collection_synthetic", fetch_event_id=event,
             evidence_qualified=True)
    f.update(cabin_class=cabin, price_source=source, source=source, source_collection={source: u})
    return f, u


def market(rt=False, econ=1200, biz=2100, arrangement="economy_all", event="E1"):
    sub = scenario.subscription()
    hard = sub["hard_constraints"]
    sub["round_trip"] = hard["round_trip"] = rt
    if not rt:
        sub.pop("return_date")
        hard.pop("return_date")
    hard.update(budget_strategy="low_price_alert", cabin_arrangement=arrangement)
    passengers = dict(PASSENGERS)
    if arrangement == "mixed":
        passengers = {"adult": 2}
        hard["cabin_allocation"] = deepcopy(ALLOC)
    analyses, data = {}, {}
    for leg in (("outbound", "return") if rt else ("outbound",)):
        fs, units = zip(*(product(leg, cabin, price, event+leg+cabin)
                          for cabin, price in (("economy", econ), ("business", biz))))
        analyses[leg] = {"roundtrip_candidate_flights": list(fs)}
        data[leg] = dict(flights=list(fs), fetch_evidence_units=list(units), source_errors=[],
                         attempted_airport_pairs=[(units[0]["origin_airport"], units[0]["dest_airport"])])
    return sub, analyses, data, dict(trip_type="round_trip" if rt else "one_way", route_type="international",
                                    origins=["PVG"], destinations=["KIX"], passengers=passengers, constraint_fp="fp")


def offer(m):
    sub, analyses, data, kw = m
    return analyzer.build_current_low_price_offer(sub, analyses, data, **kw)


def evidence(i, price=1200, *, rt=False, out=None, ret=None):
    legs = {"outbound:economy": out or f"o{i}"}
    if rt:
        legs["return:economy"] = ret or f"r{i}"
    return dict(method_version="low_price_alert_v1", bucket_key="rt" if rt else "ow", evidence_id=str(i),
                route="PVG-KIX", depart_date=DAY, return_date="2026-10-06" if rt else "",
                trip_type="round_trip" if rt else "one_way", cabin_scope="economy",
                price_scope="per_person_roundtrip" if rt else "per_person_oneway",
                amount_cny=Decimal(str(price)), amount_raw_text=str(price), can_evaluate_current=True,
                passenger_signature="adult:1", route_rate_key="international", constraint_fingerprint="fp",
                coverage_policy={}, observed_at_utc=T, observed_at_shanghai="2026-09-30T16:00:00+08:00",
                component_slots={k: dict(fetch_unit_ids=[v], unit_times={v:T}) for k,v in legs.items()})


class LocalHistoryTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.db = self.root / "prices.db"
        self.stack.enter_context(patch.object(storage, "DB_PATH", self.db))
        self.stack.enter_context(patch.object(storage, "safe_log"))

    def rows(self, sql, *args):
        with closing(sqlite3.connect(self.db)) as c, c:
            return c.execute(sql, args).fetchall()

    def count(self):
        return self.rows(f"SELECT COUNT(*) FROM {OBS}")[0][0]

    def write(self, obj, **opts):
        return storage.evaluate_and_persist_low_price_alert(obj, db_path=self.db, **opts)

    def read(self, obj):
        return storage.read_low_price_alert_evaluation_input(
            obj["bucket_key"], method_version=obj["method_version"], evidence_id=obj["evidence_id"],
            cutoff_observed_at_utc=obj["observed_at_utc"], slot_fetch_unit_map={
                k:v["fetch_unit_ids"] for k,v in obj["component_slots"].items()}, db_path=self.db)

    def test_g01_external_series_is_not_local_history(self):
        current = run_analysis("one_way", points=8, lowest=100)
        self.assertEqual((current["low_price_alert_state"], current["low_price_alert_reason"],
                          current["target_price_effective"]), ("unknown", "history_unavailable", None))
        self.assertFalse(current["low_price_alert_triggered"])
        self.assertFalse(self.db.exists())

    def test_g02_five_priors_before_first_decision(self):
        results = [self.write(evidence(i)) for i in range(6)]
        self.assertEqual([(r["state"], r["reason"], r["prior_n"]) for r in results],
                         [("unknown", "history_unavailable", 0)] +
                         [("unknown", "history_insufficient", n) for n in range(1, 5)] +
                         [("triggered", "entered_low_zone", 5)])

    def test_g03_nearest_rank_p30_before_insert(self):
        for i, p in enumerate([1000, 1100, 1200, 1300, 1400]):
            self.write(evidence(i, p))
        r = self.write(evidence(5, 1150))
        self.assertEqual((r["threshold_cny"], r["state"], r["reason"]), (1100, "not_triggered", "above_threshold"))

    def test_g04_fifth_point_cannot_confirm_itself(self):
        for i in range(5):
            self.write(evidence(i))
        r = self.write(evidence(4))
        self.assertEqual((r["prior_n"], r["reason"], r["sample_delta"], self.count()),
                         (4, "history_insufficient", 0, 5))

    def test_g05_frozen_as_of_and_partial_replay(self):
        for i in range(4):
            self.write(evidence(i, rt=True), recorded_at_utc=T)
        e = evidence(4, rt=True)
        self.write(e, recorded_at_utc=T)
        partial = evidence("partial", rt=True, out="o4")
        self.assertEqual(self.write(partial)["prior_n"], 4)
        for j, when in enumerate([T, "2026-09-29T08:00:00+00:00"]):
            other = dict(evidence(6+j, rt=True), observed_at_utc=when)
            self.write(other, recorded_at_utc="2026-10-01T08:00:00+00:00")
            for obj in (e, partial):
                r = self.write(obj)
                self.assertEqual((r["prior_n"], r["reason"], r["snapshot_reused"], r["sample_delta"]),
                                 (4, "history_insufficient", True, 0))

    def cache_setup(self):
        cache = self.root / "outer"
        request_cache.reset_for_tests(cache)
        self.addCleanup(request_cache.reset_for_tests, None)
        observations_store.set_current_round("collection_synthetic", self.root / "observations.sqlite3")
        source = juhe_source.JuheSource()
        self.source = source
        self.stack.enter_context(patch.object(juhe_source, "_cache_dir", return_value=self.root))
        self.stack.enter_context(patch.object(juhe_source, "safe_log"))
        self.stack.enter_context(patch.object(request_cache, "safe_log"))
        self.stack.enter_context(patch.dict(os.environ, {"JUHE_FLIGHT_KEY": "offline-test"}))
        self.stack.enter_context(patch.object(source, "preflight_skip", return_value=None))
        self.stack.enter_context(patch.object(source, "parse", side_effect=lambda raw: raw["result"]))
        self.stack.enter_context(patch.object(source, "normalize", side_effect=lambda rows, **kw: deepcopy(rows)))
        self.raw = {"error_code": 0, "result": [scenario.flight(True, 1200)]}
        self.http = self.stack.enter_context(patch("requests.get", return_value=Mock(
            status_code=200, text="offline", json=lambda: deepcopy(self.raw))))
        # One instant for both source responses; keep the real panel freshness check.
        instant = datetime(2026, 9, 1, 9, 0, 0)
        class FixedShanghai(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant if tz is None else instant.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(tz)
        for module in (juhe_source, request_cache, observations_store, observation_time):
            self.stack.enter_context(patch.object(module, "datetime", FixedShanghai))
        return cache

    def fetch(self, **opts):
        if opts:
            return request_cache.cached_fetch(self.source, "PVG", "KIX", DAY, include_cache_status=True, **opts)
        # Select the memory / 15-minute cache path explicitly: panel lookup normally precedes both.
        with patch.object(request_cache, "_panel_snapshot_for_key", return_value=None):
            result, status = request_cache.cached_fetch(self.source, "PVG", "KIX", DAY, include_cache_status=True)
        self.assertEqual(status, "cache")
        return result, status

    def consume(self, result):
        m = market()
        f = deepcopy(result["flights"][0])
        u = m[2]["outbound"]["fetch_evidence_units"][0]
        u.update({k:result.get(k) for k in ("fetch_event_id","observation_round_id","observed_at_utc",
                                           "evidence_qualified","source_status")})
        f.update(price_source="juhe", source_collection={"juhe": u})
        m[1]["outbound"]["roundtrip_candidate_flights"] = [f]
        o = offer(m)
        return self.write(o) if o["can_evaluate_current"] else o

    def panel(self):
        request_cache.reset_request_cache()
        result, status = self.fetch(panel_only=True)
        self.assertEqual(status, "panel")
        return result

    def check_replay(self, result, first, raw):
        r = self.consume(result)
        self.assertEqual((r["evaluated_evidence_id"], r["evaluated_amount_raw_text"], r["sample_write_status"],
                          r["sample_delta"]), (first["evaluated_evidence_id"], raw, "replayed_noop", 0))

    def test_g06_five_paths_same_verified_event(self):
        self.cache_setup()
        direct, _ = self.fetch(force_fresh=True)
        self.assertTrue(direct["panel_snapshot_written"])
        first = self.consume(direct)
        self.assertEqual((first["sample_delta"], first["evaluated_amount_raw_text"]), (1, "1200"))
        self.check_replay(self.fetch()[0], first, "1200")
        request_cache.reset_request_cache()
        self.check_replay(self.fetch()[0], first, "1200")
        self.check_replay(self.panel(), first, "1200")
        internal, _ = self.fetch(force_fresh=True)
        self.assertEqual(internal["source_status"], "cache")
        r = self.consume(internal)
        self.assertEqual((r["reason"], r["sample_delta"], self.count()), ("quote_provenance_unverified", 0, 1))
        self.assertEqual(self.http.call_count, 1)

        with self.subTest(runner_tz="UTC"):
            self.root = self.root / "utc-runner"
            self.root.mkdir()
            self.db = self.root / "prices.db"
            self.cache_setup()
            utc_instant = datetime(2026, 9, 1, 1, 0, 0)

            class FixedUtcRunner(datetime):
                @classmethod
                def now(cls, tz=None):
                    return (utc_instant if tz is None else
                            utc_instant.replace(tzinfo=timezone.utc).astimezone(tz))

                def astimezone(self, tz=None):
                    return super().astimezone(timezone.utc if tz is None else tz)

            with ExitStack() as stack:
                for module in (juhe_source, request_cache, observations_store, observation_time):
                    stack.enter_context(patch.object(module, "datetime", FixedUtcRunner))
                utc_direct, _ = self.fetch(force_fresh=True)
                self.assertEqual(utc_direct["observed_at_utc"], "2026-09-01T01:00:00+00:00")
                self.assertTrue(utc_direct["panel_snapshot_written"])
                utc_first = self.consume(utc_direct)
                self.assertEqual(utc_first["sample_delta"], 1)
                self.check_replay(self.panel(), utc_first, "1200")
                self.assertEqual(self.count(), 1)

    def test_g07_old_or_identical_raw_does_not_prove_origin(self):
        cache = self.cache_setup()
        direct, _ = self.fetch(force_fresh=True)
        self.consume(direct)
        path = next(cache.glob("api_*.json"))
        legacy = json.loads(path.read_text(encoding="utf-8"))
        for key in ("evidence_qualified", "fetch_event_id", "observation_round_id", "observed_at_utc"):
            legacy["result"].pop(key, None)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        request_cache.reset_request_cache()
        self.assertFalse(self.fetch()[0]["evidence_qualified"])
        self.source._write_cache("PVG", "KIX", DAY, "economy", deepcopy(self.raw))
        internal, _ = self.fetch(force_fresh=True)
        for result in (internal, self.panel()):
            self.assertTrue(result["flights"])
            self.assertFalse(result["evidence_qualified"])
            self.assertEqual(self.consume(result)["sample_delta"], 0)
        self.assertEqual(self.count(), 1)

    def test_g08_same_second_two_events_panel_cannot_claim_second(self):
        for p in (900, 1200):
            with self.subTest(second=p):
                self.root = self.root / str(p)
                self.root.mkdir()
                self.db = self.root / "prices.db"
                self.cache_setup()
                first, _ = self.fetch(force_fresh=True)
                self.source._cache_path("PVG", "KIX", DAY, "economy").unlink()
                self.raw["result"][0]["price"] = p
                second, _ = self.fetch(force_fresh=True)
                self.assertEqual(first["observed_at_utc"], second["observed_at_utc"])
                self.assertNotEqual(first["fetch_event_id"], second["fetch_event_id"])
                self.assertEqual((first["panel_snapshot_written"], second["panel_snapshot_written"]), (True, False))
                one, two = self.consume(first), self.consume(second)
                self.assertNotEqual(one["evaluated_evidence_id"], two["evaluated_evidence_id"])
                self.assertEqual((one["evaluated_amount_raw_text"], two["evaluated_amount_raw_text"]), ("1200", str(p)))
                self.check_replay(self.fetch()[0], two, str(p))
                request_cache.reset_request_cache()
                self.check_replay(self.fetch()[0], two, str(p))
                panel = self.panel()
                self.assertEqual(panel["flights"][0]["price"], 1200)
                self.assertFalse(panel["evidence_qualified"])
                r = self.consume(panel)
                self.assertEqual((r["state"], r["reason"], r["sample_delta"], self.count()),
                                 ("unknown", "quote_provenance_unverified", 0, 2))

    def scheduled(self, m, *, today=date(2026, 9, 30), fail_return=False):
        import plan_tracker
        sub, _, data, _ = m
        out = io.StringIO()
        seen = []
        def collect(agg, starts, ends, day, **kw):
            if fail_return and starts == ["KIX"]:
                return {"flights": [], "source_errors": [{"source": "juhe", "error": "offline failure"}]}
            batch = deepcopy(data["outbound" if starts == ["PVG"] else "return"])
            return dict(batch, collected_at=T, source_stats={}, price_insights={}, collection_freshness=[])
        with ExitStack() as stack, redirect_stdout(out):
            for name, value in {
                "_shanghai_today": today, "_departure_reference_now": datetime.combine(
                    today, datetime.min.time(), tzinfo=timezone(timedelta(hours=8))),
                "build_default_sources": ([], []), "get_constraint_epoch_boundary": None,
                "get_constraint_history_limit": 14, "get_previous_snapshot_prices": {}, "save_raw_response": None,
                "save_flight_details": None, "save_roundtrip_snapshot": None, "get_lowest_price_history": [],
                "get_roundtrip_price_history": [], "collect_nearby_dates": [], "log_signal": {},
                "system_health_check": {}, "_notification_tcurve": {}, "_notification_forecast": {},
                "_notification_provenance_context": {}, "feedback_acknowledgement": None,
                "_persist_delivered_payload": None, "record_subscription_attempt": True, "send_email": True,
                "send": True, "safe_log": None, "_log_subscription_failure": None,
                "render_detail_html": "<html></html>", "render_email": ("title","offline"),
                "render_pushplus_sections": "offline",
            }.items():
                stack.enter_context(patch.object(main, name, return_value=value))
            stack.enter_context(patch.object(main, "FlightAggregator", return_value=Mock(last_source_errors=[])))
            stack.enter_context(patch.object(main, "collect_for_airport_matrix", side_effect=collect))
            stack.enter_context(patch.object(main, "delivery_payload_with_detail_token", side_effect=deepcopy))
            stack.enter_context(patch.object(main, "_save_result_for_page", side_effect=lambda i,h,p: seen.append(p) or True))
            stack.enter_context(patch.object(main, "ANALYSIS_LOG", self.root/"analysis.log"))
            stack.enter_context(patch.object(main, "LOW_PRICE_NOTICE_STATE_PATH", self.root/"notice.json"))
            unknown = stack.enter_context(patch.object(main, "_handle_low_price_unknown"))
            stack.enter_context(patch.object(plan_tracker, "DEFAULT_DATA_DIR", self.root/"plans"))
            stack.enter_context(patch.object(plan_tracker, "DEFAULT_FEEDBACK_PATH", self.root/"feedback"))
            for name in ("get_last_push_price","get_last_push_snapshot"):
                stack.enter_context(patch.object(notifier, name, return_value=None))
            db_init = stack.enter_context(patch.object(main, "init_db"))
            ok = main._process_scheduled_subscription(sub, preflight={"skip":False}, round_id="collection_synthetic")
            self.assertIs(ok, True, out.getvalue())
            db_init.assert_not_called()
            return seen, unknown

    def test_g09_scheduled_entry_lazily_records_only_normal_low_price(self):
        normal = market()
        normal[0]["hard_constraints"]["budget_strategy"] = "explicit"
        self.scheduled(normal)
        self.scheduled(market(rt=True), today=date(2026,10,2))
        self.assertFalse(self.db.exists())
        low = market()
        self.scheduled(low)
        self.assertTrue(self.db.exists(), "scheduled low-price receipt was not persisted")
        self.assertEqual(self.count(), 1)
        self.scheduled(low)
        self.assertEqual(self.count(), 1)

    def test_g10_legacy_init_empty_batch_and_normal_entry_do_not_create_schema(self):
        storage.init_db()
        storage.get_lowest_price_history("PVG-KIX", DAY)
        with (patch.object(main,"load_file_subscriptions",return_value=[]),
              patch.object(main,"require_subscriptions_file"), patch.object(main,"safe_log"),
              patch.object(main,"_run_basket_sentinel_for_main")):
            main._run_locked(sync_remote=False, round_id="collection_synthetic")
        normal = market()
        normal[0]["hard_constraints"]["budget_strategy"]="explicit"
        self.scheduled(normal)
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name LIKE 'low_price_alert_%'"), [])
        self.assertEqual(self.read(evidence(1))["prior_rows"], [])
        self.write(evidence(1))
        self.assertEqual(self.count(), 1)

    def test_g11_reader_and_writer_reject_damaged_history_without_repair(self):
        o = evidence(1, rt=True)
        def protect(error):
            before = self.db.read_bytes()
            with self.assertRaises(error):
                self.read(o)
            with self.assertRaises(error):
                self.write(evidence(2, rt=True, ret="r1"))
            self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.read(o)["prior_rows"], [])
        self.assertFalse(self.db.exists())
        for raw in (b"", b"invalid database"):
            self.db.write_bytes(raw)
            protect(storage.LowPriceStorageReadError)
        self.db.unlink()
        storage.init_db()
        before = self.db.read_bytes()
        self.assertEqual(self.read(o)["prior_rows"], [])
        self.assertEqual(self.db.read_bytes(), before)
        for n, statements in enumerate((
            [f"DROP TABLE {CLAIMS}", f"DROP TABLE {EVAL}"], [f"DROP TABLE {CLAIMS}"],
            [f"DELETE FROM {CLAIMS} WHERE slot_key='return:economy'"], [f"DELETE FROM {EVAL}"],
            [f"ALTER TABLE {EVAL} RENAME COLUMN prior_n TO bad"],
            [f"CREATE TABLE scratch AS SELECT * FROM {CLAIMS}", f"DROP TABLE {CLAIMS}",
             f"ALTER TABLE scratch RENAME TO {CLAIMS}"],
        )):
            with self.subTest(case=n):
                self.db = self.root / f"schema-{n}.db"
                self.write(o)
                for sql in statements:
                    self.rows(sql)
                protect(storage.LowPriceStorageSchemaError)

    def test_g12_decimal_identity_does_not_round_thirty_digits(self):
        self.write(evidence("one", "1.0"))
        self.assertEqual(self.write(evidence("one", "1.00"))["sample_delta"], 0)
        a,b = ("1." + "0"*28 + str(i) for i in (1,2))
        self.write(evidence("a", a))
        self.write(evidence("b", b))
        self.assertEqual(self.rows(f"SELECT amount_raw_text FROM {OBS} ORDER BY id"), [("1",), (a,), (b,)])
        with self.assertRaises(storage.LowPriceEvidenceConflictError):
            self.write(evidence("a", b))
        self.assertEqual(self.count(), 3)

    def test_g13_invalid_raw_amount_projection_and_scope_rejected_before_write(self):
        for amount,raw in [(0,"0"),(-1,"-1"),(float("nan"),"1"),(float("inf"),"1"),(-float("inf"),"1"),
                           (True,"1"),(1200,"¥1,200"),(1,"１.００"),(1.0,"1.00000000000000001")]+[
                           (Decimal(v),v) for v in ("1"+"0"*400, "0."+"0"*400+"1")]:
            with self.subTest(raw=raw), self.assertRaises(storage.LowPriceInvalidAmountError):
                self.write(dict(evidence(1),amount_cny=amount,amount_raw_text=raw))
        with self.assertRaises(storage.LowPriceInvalidAmountError):
            self.write(dict(evidence(1),price_scope="per_person_roundtrip"))
        self.assertFalse(self.db.exists())

    def test_g14_single_cabin_isolation_in_both_directions(self):
        for rt in (False,True):
            factor = 2 if rt else 1
            a = offer(market(rt,biz=7000))
            b = offer(market(rt,biz=10))
            c = offer(market(rt,econ=5,arrangement="business_all"))
            d = offer(market(rt,econ=9000,arrangement="business_all"))
            self.assertEqual((a["amount_cny"],b["amount_cny"]), (1200*factor,1200*factor))
            self.assertEqual((c["amount_cny"],d["amount_cny"]), (2100*factor,2100*factor))
            self.assertTrue(all(f["cabin_class"]=="business" for f in c["selected_flights"].values()))
            self.assertNotEqual(a["bucket_key"],c["bucket_key"])
            self.write(a)
            self.assertEqual(self.write(c)["prior_n"],0)

    def test_g15_ambiguous_target_is_not_priced(self):
        for arrangement,policy in [("level_based","business_allowed"),("","business_allowed")]:
            m = market(arrangement=arrangement)
            m[0]["hard_constraints"]["cabin_policy"]=policy
            r = offer(m)
            self.assertEqual((r["state"],r["reason"],r["sample_delta"]),("unknown","cabin_scope_ambiguous",0))

    def test_g16_oneway_mixed_raw_total_not_display_rounding(self):
        m = market(econ=101.25,biz=200.125,arrangement="mixed")
        r = offer(m)
        self.assertEqual((r["price_scope"],r["amount_cny"]),("all_passengers_oneway",Decimal("301.375")))
        self.assertEqual(self.write(r)["evaluated_amount_raw_text"],"301.375")
        self.assertEqual(set(r["component_slots"]),{"outbound:economy","outbound:business"})

    def test_g17_strict_roundtrip_all_four_slots_required(self):
        m = market(rt=True, arrangement="mixed")
        r = offer(m)
        self.assertEqual((r["price_scope"],r["amount_cny"],len(r["component_slots"])),
                         ("all_passengers_roundtrip",6600,4))
        for change in ("missing","date","airport","absent_allocation","bad_allocation"):
            with self.subTest(change=change):
                x = deepcopy(m)
                pool = x[1]["return"]["roundtrip_candidate_flights"]
                if change == "missing":
                    pool.pop()
                elif change == "date":
                    pool[1]["segments"][0]["dep_time"]="2026-10-05 13:00"
                elif change == "airport":
                    pool[1]["segments"][0]["dep_airport"]="NRT"
                elif change == "absent_allocation":
                    del x[0]["hard_constraints"]["cabin_allocation"]
                else:
                    x[0]["hard_constraints"]["cabin_allocation"]["economy"]["adult"]=10
                self.assertEqual(offer(x)["reason"],"invalid_cabin_allocation" if "allocation" in change else "no_valid_offers")

    def test_g18_positive_arrival_and_only_relevant_source_errors(self):
        for arrangement,missing in (("economy_all","juhe"),("business_all","serpapi"),("mixed","juhe"),
                                    ("mixed","serpapi")):
            m = market(arrangement=arrangement)
            key=offer(m)["bucket_key"]
            for status in ("absent","not_configured","failed_quota","skipped_date","error"):
                x=deepcopy(m)
                d=x[2]["outbound"]
                units=d["fetch_evidence_units"]
                u=next(u for u in units if u["source"]==missing)
                if status=="absent":
                    units.remove(u)
                elif status=="error":
                    d["source_errors"]=[dict(u,error="failed")]
                else:
                    u["source_status"]=status
                r=offer(x)
                self.assertEqual((r["bucket_key"],r["reason"],r["sample_delta"]),(key,"source_coverage_incomplete",0))
        m=market()
        m[2]["outbound"]["source_errors"]=[dict(source="serpapi",cabin_class="business"),dict(source="duffel")]
        m[2]["outbound"]["fetch_evidence_units"].pop()
        self.assertTrue(offer(m)["coverage_complete"])
        self.assertEqual(self.write(offer(m))["sample_delta"],1)
        errors=[]
        for leg,origin in (("outbound","PVG"),("outbound","SHA"),("return","KIX")):
            main._merge_source_errors(errors,[dict(source="juhe",error="unavailable",cabin_class="economy")],
                                      leg=leg,origin_airport=origin,dest_airport="X")
        self.assertEqual([(e["leg"],e["origin_airport"]) for e in errors],
                         [("outbound","PVG"),("outbound","SHA"),("return","KIX")])

    def test_g19_tied_flights_airports_and_sources_are_order_independent(self):
        m=market()
        pool=m[1]["outbound"]["roundtrip_candidate_flights"]
        data=m[2]["outbound"]
        m[3]["origins"].append("SHA")
        data["attempted_airport_pairs"].append(("SHA","KIX"))
        u0=data["fetch_evidence_units"][0]
        for airport,no in (("PVG","CA123"),("SHA","AA200")):
            for name in ("juhe","second"):
                u=dict(u0, source=name, origin_airport=airport, fetch_event_id=airport+name)
                data["fetch_evidence_units"].append(u)
                f=deepcopy(pool[0])
                f.update(flight_combo=no, departure_airport=airport, price_source=name,source_collection={name:u})
                f["segments"][0].update(flight_no=no,dep_airport=airport)
                pool.append(f)
        specs={"sources":[{"name":s,"role":"primary","weight":1,"cabins":["economy"]} for s in ("juhe","second")]}
        with patch.dict(source_profiles.ROUTE_SOURCE_PROFILES, {"international":specs}):
            first=offer(m)
            self.write(first)
            for seq in (pool,data["attempted_airport_pairs"],data["fetch_evidence_units"],specs["sources"]):
                seq.reverse()
            second=offer(m)
            self.assertEqual(first["evidence_id"],second["evidence_id"])
            self.assertEqual(first["selected_flights"],second["selected_flights"])
            self.assertEqual(second["component_slots"]["outbound:economy"]["tied_price_sources"],["juhe","second"])
            self.assertEqual((self.write(second)["sample_delta"], self.count()),(0,1))

    def test_g20_passenger_scope_rate_and_constraint_buckets(self):
        m=market()
        keys=[offer(m)["bucket_key"]]
        m[0]["hard_constraints"]["budget_scope"]="all"
        keys.append(offer(m)["bucket_key"])
        m[3]["passengers"]={"adult":1,"child":1}
        international=offer(m)
        keys.append(international["bucket_key"])
        m[3]["route_type"]="domestic"
        domestic=offer(m)
        keys.append(domestic["bucket_key"])
        self.assertEqual((international["amount_cny"],domestic["amount_cny"]),(2100,1800))
        m[3]["constraint_fp"]="different"
        keys.append(offer(m)["bucket_key"])
        self.assertEqual(len(set(keys)),len(keys))

    def test_g21_slot_claims_and_first_versus_current_write(self):
        one=evidence(1,rt=True)
        partial=evidence("mix",rt=True,out="o1",ret="r2")
        def check(obj,first,status,delta):
            r=self.write(obj)
            self.assertEqual((r["first_sample_write_status"],r["sample_write_status"],r["sample_delta"],
                              r["snapshot_reused"]),(first,status,delta,status=="replayed_noop"))
        check(one,"inserted","inserted",1)
        check(one,"inserted","replayed_noop",0)
        check(partial,"skipped_partial_reuse","skipped_partial_reuse",0)
        check(evidence(2,rt=True),"inserted","inserted",1)
        check(partial,"skipped_partial_reuse","replayed_noop",0)
        self.assertEqual(self.count(),2)

    def test_g22_legacy_history_and_panel_are_not_backfilled(self):
        f=scenario.flight(True,1200)
        storage.save_flight_details("PVG-KIX",DAY,[f])
        storage.save_roundtrip_snapshot("PVG-KIX",DAY,"2026-10-06",1200,900,2100,T)
        observations_store.append_observations([f], source="juhe", origin_airport="PVG", dest_airport="KIX",
                depart_date=DAY, cabin_class="economy", observed_at=T, round_id="legacy", route_type="international",
                db_path=self.root/"observations.sqlite3")
        old=storage.get_lowest_price_history("PVG-KIX",DAY)
        self.assertTrue(old)
        self.assertEqual(self.read(evidence(1))["prior_rows"],[])
        self.assertEqual(self.write(evidence(1))["prior_n"],0)
        self.assertEqual(storage.get_lowest_price_history("PVG-KIX",DAY),old)

    def test_g23_notification_amount_plan_and_sample_size_have_one_source(self):
        import plan_tracker
        self.stack.enter_context(patch.object(plan_tracker,"DEFAULT_DATA_DIR",self.root/"plans"))
        for rt in (False,True):
            for arrangement in ("economy_all","business_all","mixed"):
                with self.subTest(rt=rt,arrangement=arrangement):
                    for i in range(5):
                        self.write(offer(market(rt,econ=1010+i,biz=2520+i,arrangement=arrangement,event=str(i))))
                    m=market(rt,econ=810,biz=2280,arrangement=arrangement,event="now")
                    o=offer(m)
                    ctx=dict(o, **self.write(o))
                    a=dict(m[1]["outbound"], budget_strategy="low_price_alert",low_price_alert_context=ctx)
                    result=notifier.build_local_low_price_payload(analysis_result=a,subscription=m[0],
                            route_info=m[0],price_insights={"lowest_price":987654,"price_history":[["x",987654]]*8},
                            return_analysis=m[1].get("return"))
                    self.assertEqual(result["push_type"],"进入低价区间")
                    self.assertEqual((result["ideal_price"],result["verify_price"],result["price_signal"]["sample_n"]),
                                     (ctx["threshold_cny"],ctx["threshold_cny"],5))
                    plan=result["recommended_plans"][0]
                    self.assertEqual(plan["price"],o["amount_cny"])
                    self.assertEqual(plan["low_price_slots"],o["selected_flights"])
                    self.assertIn(ctx["threshold_raw_text"],result["buy_condition"])
                    self.assertNotIn("987654",result["recommendation"])
                    if arrangement=="mixed":
                        self.assertIn(notifier.MIXED_CABIN_DISCLOSURE,plan["summary"])

    def test_g24_non_low_price_panel_display_and_incomplete_roundtrip_delivery(self):
        self.cache_setup()
        self.source._write_cache("PVG","KIX",DAY,"economy",self.raw)
        cached,_=self.fetch(force_fresh=True)
        self.assertEqual(cached["source_status"],"cache")
        panel=self.panel()
        self.assertTrue(panel["flights"])
        self.assertFalse(panel["evidence_qualified"])
        m=market()
        f=deepcopy(panel["flights"][0])
        u=m[2]["outbound"]["fetch_evidence_units"][0]
        u.update({k:panel.get(k) for k in ("fetch_event_id","observation_round_id","observed_at_utc",
                                           "evidence_qualified","source_status")})
        f.update(source_collection={"juhe":u},price_source="juhe")
        m[2]["outbound"]["flights"]=[f]
        m[0]["hard_constraints"]["budget_strategy"]="explicit"
        sent,_=self.scheduled(m)
        self.assertTrue(sent)
        m[0]["hard_constraints"]["budget_strategy"]="low_price_alert"
        sent,unknown=self.scheduled(m)
        self.assertFalse(sent)
        self.assertEqual(unknown.call_args.args[1]["low_price_alert_reason"],"quote_provenance_unverified")
        self.assertFalse(self.db.exists())
        sent,unknown=self.scheduled(market(rt=True),fail_return=True)
        unknown.assert_not_called()
        self.assertEqual([p["push_type"] for p in sent],["数据不完整"])
        self.assertFalse((self.root/"notice.json").exists())
