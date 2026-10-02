"""Offline contracts for fixed-departure descriptive trajectories."""
import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import date
from unittest.mock import patch

from tcurve import load_tcurve_daily_cells


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "trajectory_report.py"
DEPARTURES = ("2026-09-08", "2026-10-01")
ROUTE = "上海-大阪"


def make_snapshot(root):
    root.mkdir()
    with closing(sqlite3.connect(root / "observations.sqlite3")) as db:
        db.executescript("""
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY, observed_at TEXT, observed_at_utc TEXT,
              observed_day_shanghai TEXT, legacy_time_ambiguous INTEGER,
              round_id TEXT, route_type TEXT, origin_airport TEXT,
              dest_airport TEXT, depart_date TEXT, days_to_departure INTEGER,
              cabin_class TEXT, source TEXT, flight_combo TEXT, price_cny REAL);
            CREATE TABLE collection_cells (
              id INTEGER PRIMARY KEY, round_id TEXT, request_fingerprint TEXT,
              source TEXT, origin_airport TEXT, dest_airport TEXT, depart_date TEXT,
              cabin_class TEXT, observed_day_shanghai TEXT, sample_role TEXT,
              cohort_id TEXT, route_type TEXT, execution_status TEXT,
              raw_result_count INTEGER, valid_result_count INTEGER,
              skip_reason_code TEXT, error_type TEXT, error_code TEXT,
              UNIQUE(round_id, request_fingerprint));
        """)

        def quote(day, source, price, rid, *, dep=DEPARTURES[0], kind="international",
                  combo=None, timestamp=None, explicit=True, ambiguous=0):
            db.execute("INSERT INTO observations VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (timestamp or day+"T09:00:00+08:00", None,
                        day if explicit else None, ambiguous, rid, kind, "PVG", "KIX",
                        dep, (date.fromisoformat(dep)-date.fromisoformat(day)).days,
                        "economy", source, combo or rid+"-flight", price))

        def ledger(day, source, status, rid, *, dep=DEPARTURES[0], role="trajectory_anchor",
                   count=1, kind="international"):
            db.execute("INSERT INTO collection_cells VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (rid, rid+"-"+source, source, "PVG", "KIX", dep, "economy", day,
                        role, "synthetic", kind, status, count, count, None, None, None))

        quote("2026-08-09", "juhe", 200, "legacy-j")
        quote("2026-08-09", "hasdata", 210, "legacy-h")
        for source, price, rid, status in (("juhe",80,"valid-reuse","reused"),
                                          ("hasdata",100,"valid-h","success"),
                                          ("juhe",120,"valid-j","success")):
            quote("2026-08-10",source,price,rid)
            ledger("2026-08-10",source,status,rid)
        quote("2026-08-11","juhe",90,"degraded")
        ledger("2026-08-11","juhe","success","degraded")
        for source,price,rid in (("juhe",100.001,"tie-j"),
                                 ("hasdata",100.001,"tie-h"),
                                 ("juhe",100.004,"display-only")):
            quote("2026-08-12",source,price,rid)
            ledger("2026-08-12",source,"success",rid)
        for source in ("juhe","hasdata"):
            ledger("2026-08-13",source,"empty","empty",count=0)
        ledger("2026-08-14","juhe","running","pending",count=0)
        quote("2026-08-15","hasdata",130,"switch")
        ledger("2026-08-15","hasdata","success","switch")
        ledger("2026-08-15","juhe","empty","switch-empty",count=0)
        for kind,price in (("international",140),("domestic",150)):
            quote("2026-08-16","juhe",price,"mixed-"+kind,kind=kind)
            ledger("2026-08-16","juhe","success","mixed-"+kind,kind=kind)
        quote("2026-08-17","juhe",160,"cross-day",
              timestamp="2026-08-16T18:00:00+00:00",explicit=False)
        quote("2026-08-17","juhe",1,"ambiguous",ambiguous=1)
        ledger("2026-08-17","juhe","success","cross-day")
        quote("2026-09-02","juhe",180,"second",dep=DEPARTURES[1])
        ledger("2026-09-02","juhe","success","second",dep=DEPARTURES[1])
        quote("2026-09-13","juhe",190,"user-only",dep=DEPARTURES[1])
        ledger("2026-09-13","juhe","success","user-only",dep=DEPARTURES[1],role="user_monitor")
        ledger("2026-08-30","juhe","running","outside",dep="2026-10-11",count=0)
        db.commit()
    snapshot_sha = hashlib.sha256((root/"observations.sqlite3").read_bytes()).hexdigest()
    (root/"snapshot_manifest.json").write_text(json.dumps({
        "label":"synthetic-contract", "snapshot_sha256":{"observations.sqlite3":snapshot_sha}
    }),encoding="utf-8")
    return hashlib.sha256((root/"snapshot_manifest.json").read_bytes()).hexdigest()


def load_module():
    if not SCRIPT.is_file():
        raise AssertionError("ENTRYPOINT_NOT_PRESENT: scripts/trajectory_report.py")
    spec=importlib.util.spec_from_file_location("trajectory_contract_subject",SCRIPT)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mutant(kind):
    module=load_module()
    tree=ast.parse(SCRIPT.read_text(encoding="utf-8"))
    old=ast.dump(tree,include_attributes=False)
    functions={n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
    hits=[]

    def assignment(function, target):
        matches=[n for n in ast.walk(functions[function]) if isinstance(n,ast.Assign)
                 and any(isinstance(t,ast.Name) and t.id==target for t in n.targets)]
        if len(matches)!=1:
            raise AssertionError("MUTATION_NODE_MATCH:"+target)
        hits.extend(matches)
        return matches[0]

    if kind=="admit_degraded":
        assignment("_build_row","strict").value=ast.parse("cell is not None",mode="eval").body
    elif kind=="success_price":
        assignment("_build_row","strict_price").value=ast.parse(
            "_mutation_success_price(raw_rows, ledger_rows) if strict else None",mode="eval").body
    elif kind=="lose_tuple":
        matches=[n for n in ast.walk(functions["_minimum_evidence"]) if isinstance(n,ast.Tuple)
                 and len(n.elts)==3 and all(isinstance(e,ast.Subscript) for e in n.elts)
                 and "flight_combo" in ast.unparse(n)]
        if len(matches)!=1:
            raise AssertionError("MUTATION_NODE_MATCH:minimum_tuple")
        hits.extend(matches)
        matches[0].elts[2]=ast.Constant(value=None)
    elif kind=="fill_zero":
        assignment("_build_row","strict_price").value.orelse=ast.Constant(value=0)
    elif kind=="slice_day":
        matches=[n for n in ast.walk(functions["_load_evidence"]) if isinstance(n,ast.Assign)
                 and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name)
                 and n.value.func.id=="resolve_observed_day_shanghai"]
        if len(matches)!=1:
            raise AssertionError("MUTATION_NODE_MATCH:day_resolver")
        hits.extend(matches)
        matches[0].value=ast.parse('(str(row.get("observed_at") or "")[:10], "mutated")',mode="eval").body
    elif kind=="inflate_n":
        assignment("_counts_by_t","n").value=ast.parse(
            'sum(len(row["round_ids"])*len(row["priced_sources"]) for row in items)',mode="eval").body
    elif kind=="median":
        returns=[n for n in functions["_comparison_lines"].body if isinstance(n,ast.Return)]
        if len(returns)!=1:
            raise AssertionError("MUTATION_NODE_MATCH:comparison_return")
        hits.extend(returns)
        functions["_comparison_lines"].body.insert(-1,ast.parse('lines.append("中位数: 100")').body[0])
    elif kind=="split_mixed":
        returns=[n for n in functions["_trajectory_rows"].body if isinstance(n,ast.Return)]
        if len(returns)!=1:
            raise AssertionError("MUTATION_NODE_MATCH:rows_return")
        hits.extend(returns)
        returns[0].value=ast.parse("_mutation_split_rows(rows)",mode="eval").body
    else:
        raise AssertionError("UNKNOWN_MUTATION")
    if len(hits)!=1 or ast.dump(tree,include_attributes=False)==old:
        raise AssertionError("MUTATION_NO_CHANGE")
    ast.fix_missing_locations(tree)
    compiled=compile(tree,str(SCRIPT),"exec")
    scope={"__file__":str(SCRIPT),"__name__":"trajectory_mutant"}
    exec(compiled,scope)

    def success_price(raw_rows,ledger_rows):
        successes={(r["source"],r["round_id"]) for r in ledger_rows if r["execution_status"]=="success"}
        return min((float(r["price_cny"]) for r in raw_rows if (r["source"],r["round_id"]) in successes),default=None)

    def split_rows(rows):
        result=[]
        for row in rows:
            for _ in row["route_types"] if len(row["route_types"])>1 else [None]:
                result.append(copy.deepcopy(row))
        return result

    scope.update(_mutation_success_price=success_price,_mutation_split_rows=split_rows)
    module.__dict__.update(scope)
    return module


class TrajectoryReportTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.snapshot=Path(temp.name)/"snapshot"
        self.sha=make_snapshot(self.snapshot)

    def invoke(self,module=None):
        module=module or load_module()
        before={p.name:p.read_bytes() for p in self.snapshot.iterdir()}
        connect=sqlite3.connect
        builtin_open=open
        io_open=io.open
        os_open=os.open
        connections=[]

        def readonly(*args,**kwargs):
            self.assertTrue(kwargs.get("uri"),"READONLY_URI_REQUIRED")
            self.assertIn("?mode=ro",str(args[0]),"READONLY_URI_REQUIRED")
            connections.append(str(args[0]))
            return connect(*args,**kwargs)

        def checked(opener):
            def call(file,mode="r",*args,**kwargs):
                self.assertFalse(any(c in str(mode) for c in "wa+x"),"REPORT_FILE_WRITE")
                return opener(file,mode,*args,**kwargs)
            return call

        def checked_os_open(path,flags,*args,**kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND),"REPORT_OS_WRITE")
            return os_open(path,flags,*args,**kwargs)

        with patch.object(sqlite3,"connect",side_effect=readonly), \
                patch.object(socket,"socket",side_effect=AssertionError("REPORT_NETWORK")), \
                patch("builtins.open",side_effect=checked(builtin_open)), \
                patch.object(io,"open",side_effect=checked(io_open)), \
                patch.object(os,"open",side_effect=checked_os_open), \
                patch.object(os,"unlink",side_effect=AssertionError("REPORT_UNLINK")), \
                patch.object(os,"rename",side_effect=AssertionError("REPORT_RENAME")):
            value=module.generate_report(db_path=self.snapshot,expect_manifest_sha=self.sha,
                                         route=ROUTE,airport_pair="PVG-KIX",departures=DEPARTURES)
        self.assertTrue(connections,"REAL_READ_REQUIRED")
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.snapshot.iterdir()},"SNAPSHOT_CHANGED")
        return value

    def row(self,data,day,dep=DEPARTURES[0]):
        return next(r for t in data["trajectories"] if t["depart_date"]==dep for r in t["rows"] if r["observed_day"]==day)

    def assert_strict(self,module=None):
        _,data=self.invoke(module)
        for cell in load_tcurve_daily_cells(self.snapshot/"observations.sqlite3",route=ROUTE,airport_pair="PVG-KIX"):
            row=self.row(data,cell["observed_day"],cell["depart_date"])
            expected=cell["min_price"] if not cell["degraded"] else None
            self.assertEqual(row["strict_price"],expected,"STRICT_CELL_VALUE")

    def assert_degraded(self,module=None):
        text,data=self.invoke(module)
        row=self.row(data,"2026-08-11")
        self.assertIsNone(row["strict_price"],"DEGRADED_NOT_STRICT")
        self.assertEqual(row["supplementary_price"],90,"SUPPLEMENTARY_PRICE")
        self.assertIn("当时期望",text)
        self.assertIn("实际",text)
        self.assertIn("当时期望双源、实际单源",text)
        self.assertEqual(row["expected_sources"],["hasdata","juhe"])

    def assert_trace(self,module=None):
        _,data=self.invoke(module)
        row=self.row(data,"2026-08-12")
        self.assertEqual(set(row["minimum_tuples"]),{("juhe","tie-j","tie-j-flight"),("hasdata","tie-h","tie-h-flight")},"MINIMUM_TUPLES")
        cross=self.row(data,"2026-08-17")
        self.assertEqual(cross["minimum_tuples"],[("juhe","cross-day","cross-day-flight")],"SHANGHAI_MINIMUM_TUPLES")
        self.assertEqual(cross["strict_price"],160)

    def assert_no_price(self,module=None):
        _,data=self.invoke(module)
        for day in ("2026-08-13","2026-08-14"):
            row=self.row(data,day)
            self.assertIsNone(row["strict_price"],"NO_PRICE_STRICT_BLANK")
            self.assertIsNone(row["supplementary_price"],"NO_PRICE_SUPPLEMENT_BLANK")
            self.assertIn("无价格",row["gaps"])
        pending=self.row(data,"2026-08-14")
        self.assertIn("终态未闭合",pending["gaps"])
        self.assertIn("质量排除",pending["gaps"])

    def assert_n(self,module=None):
        _,data=self.invoke(module)
        self.assertEqual(data["n_by_t"][29],2,"TRAJECTORY_N")
        self.assertTrue(all(0<=n<=2 for n in data["n_by_t"].values()),"TRAJECTORY_N_BOUND")

    def assert_comparison(self,module=None):
        text,_=self.invoke(module)
        comparison=text.split("轨迹并列描述",1)[1]
        for term in ("均值","中位数","购票时点"):
            self.assertNotIn(term,comparison,"DESCRIPTIVE_ONLY")

    def assert_mixed(self,module=None):
        text,data=self.invoke(module)
        rows=[r for t in data["trajectories"] for r in t["rows"] if r["observed_day"]=="2026-08-16"]
        self.assertEqual(len(rows),1,"MIXED_NOT_SPLIT")
        self.assertIsNone(rows[0]["unit"],"MIXED_UNIT_UNPROVEN")
        self.assertEqual(rows[0]["route_types"],["domestic","international"])
        self.assertIn("route_type不一致",text)

    def test_c0_existing_daily_cell_reference(self):
        cells=load_tcurve_daily_cells(self.snapshot/"observations.sqlite3",route=ROUTE,airport_pair="PVG-KIX")
        by_day={c["observed_day"]:c for c in cells}
        self.assertEqual(by_day["2026-08-10"]["collection_state"],"valid")
        self.assertEqual(by_day["2026-08-10"]["min_price"],80)
        self.assertTrue(by_day["2026-08-11"]["degraded"])
        self.assertEqual(by_day["2026-08-09"]["collection_state"],"legacy")

    def test_c1_strict_values_match_existing_cells(self):
        self.assert_strict()

    def test_c2_degraded_only_supplementary(self):
        self.assert_degraded()

    def test_c3_exact_ties_and_shanghai_provenance(self):
        self.assert_trace()
        with closing(sqlite3.connect(self.snapshot/"observations.sqlite3")) as db:
            db.execute("ALTER TABLE observations RENAME COLUMN id TO hidden_id")
            db.commit()
        manifest = self.snapshot/"snapshot_manifest.json"
        sealed = json.loads(manifest.read_bytes())
        sealed["snapshot_sha256"]["observations.sqlite3"] = hashlib.sha256(
            (self.snapshot/"observations.sqlite3").read_bytes()).hexdigest()
        manifest.write_text(json.dumps(sealed),encoding="utf-8")
        self.sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
        _,data=self.invoke()
        self.assertEqual(self.row(data,"2026-08-12")["minimum_tuples"],[])
        self.assertIn("证据不足",self.row(data,"2026-08-12")["minimum_evidence"])

    def test_c4_success_is_not_reused_or_empty(self):
        _,data=self.invoke()
        row=self.row(data,"2026-08-10")
        self.assertEqual(row["successes"],[("hasdata","valid-h"),("juhe","valid-j")])
        self.assertEqual(self.row(data,"2026-08-09")["successes"],[])
        self.assertEqual(self.row(data,"2026-08-09")["success_evidence"],"未记录")
        self.assertEqual(self.row(data,"2026-08-13")["successes"],[])

    def test_c5_gaps_and_switches(self):
        self.assert_no_price()
        _,data=self.invoke()
        self.assertIn("来源切换",self.row(data,"2026-08-12")["switches"])
        self.assertIn("组合切换",self.row(data,"2026-08-12")["switches"])
        self.assertEqual(self.row(data,"2026-08-15")["switches"],[])
        self.assertIn("角色缺项",self.row(data,"2026-09-13",DEPARTURES[1])["gaps"])
        self.assertNotIn("角色缺项",self.row(data,"2026-08-09")["gaps"])

    def test_c6_running_not_rewritten(self):
        self.assert_no_price()
        _,data=self.invoke()
        self.assertEqual(self.row(data,"2026-08-14")["execution_statuses"],{"running":1})

    def test_c7_n_counts_trajectories(self):
        self.assert_n()

    def test_c8_comparison_is_descriptive(self):
        self.assert_comparison()

    def test_c9_mixed_route_type_not_split(self):
        self.assert_mixed()

    def test_c10_manifest_gate_and_required_arguments(self):
        module=load_module()
        with redirect_stdout(io.StringIO()) as success:
            self.assertEqual(module.main(["--db",str(self.snapshot/"observations.sqlite3"),
                "--route",ROUTE,"--pair","PVG-KIX","--depart",DEPARTURES[0],
                "--expect-manifest-sha",self.sha]),0)
        self.assertIn("固定出发日",success.getvalue())
        with redirect_stdout(io.StringIO()) as output:
            code=module.main(["--db",str(self.snapshot),"--route",ROUTE,"--pair","PVG-KIX",
                              "--depart",DEPARTURES[0],"--expect-manifest-sha","0"*64])
        self.assertNotEqual(code,0,"MANIFEST_MISMATCH_MUST_FAIL")
        self.assertIn("manifest",output.getvalue().lower())
        with redirect_stdout(io.StringIO()), patch("sys.stderr",new_callable=io.StringIO), self.assertRaises(SystemExit):
            module.main(["--route",ROUTE,"--pair","PVG-KIX","--depart",DEPARTURES[0]])

    def test_c11_static_and_runtime_readonly(self):
        module=load_module()
        tree=ast.parse(SCRIPT.read_text(encoding="utf-8"))
        denied={"write_text","write_bytes","mkdir","unlink","remove","rename","replace",
                "open","touch","rmdir","rmtree","truncate","write","writelines","makedirs","chmod",
                "create_runtime_backup","restore_runtime_backup","rehearse_runtime_backup",
                "restore_to_production","record_backup_created","record_restore_verified",
                "load_daily_collection_state","managed_observation_connection","connect"}
        for n in ast.walk(tree):
            if isinstance(n,ast.Call):
                name=n.func.attr if isinstance(n.func,ast.Attribute) else n.func.id if isinstance(n.func,ast.Name) else ""
                self.assertNotIn(name,denied,"FORBIDDEN_REPORT_CALL")
            if isinstance(n,(ast.Import,ast.ImportFrom)):
                names=[a.name for a in n.names]+([n.module] if isinstance(n,ast.ImportFrom) else [])
                self.assertFalse(set(names)&{"collection_ledger","runtime_backup","runtime_restore","backup_status","main","web_form"})
        self.assertNotIn("DEFAULT_DB_PATH",SCRIPT.read_text(encoding="utf-8"))
        self.invoke(module)

    def test_footer_counts_do_not_claim_real_calls(self):
        text,data=self.invoke()
        self.assertIsNone(data["ledger_counts"]["actual_sent"])
        self.assertIn("未记录/不能确定",text)
        self.assertEqual(data["ledger_counts"]["statuses"]["running"],1)
        self.assertEqual(data["ledger_counts"]["statuses"]["reused"],1)
        self.assertEqual(data["ledger_counts"]["statuses"]["empty"],3)

    def test_c12_mutations_fail_target_assertions(self):
        cases=(("admit_degraded",self.assert_strict,"STRICT_CELL_VALUE"),
               ("admit_degraded",self.assert_degraded,"DEGRADED_NOT_STRICT"),
               ("success_price",self.assert_strict,"STRICT_CELL_VALUE"),
               ("lose_tuple",self.assert_trace,"MINIMUM_TUPLES"),
               ("fill_zero",self.assert_no_price,"NO_PRICE_STRICT_BLANK"),
               ("slice_day",self.assert_trace,"SHANGHAI_MINIMUM_TUPLES"),
               ("inflate_n",self.assert_n,"TRAJECTORY_N"),
               ("median",self.assert_comparison,"DESCRIPTIVE_ONLY"),
               ("split_mixed",self.assert_mixed,"MIXED_NOT_SPLIT"))
        for kind,check,reason in cases:
            with self.subTest(mutation=kind,assertion=reason):
                mutant=_mutant(kind)
                with self.assertRaisesRegex(AssertionError,reason):
                    check(mutant)


if __name__=="__main__":
    unittest.main()
