import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _increment_probe(path_text: str, start_event, result_queue) -> None:
    from research_state_store import (
        ResearchStateConflict,
        load_research_state,
        update_research_state,
    )

    path = Path(path_text)
    start_event.wait(timeout=10)
    for _attempt in range(20):
        state = load_research_state(path)
        revision = state["revision"]

        def mutate(payload):
            cohort = payload.setdefault("research_cohort_v2", {})
            cohort["probe_updates"] = int(cohort.get("probe_updates") or 0) + 1
            return payload

        try:
            update_research_state(path, revision, mutate)
            result_queue.put("updated")
            return
        except ResearchStateConflict:
            continue
    result_queue.put("conflict")


class ResearchStateStoreTest(unittest.TestCase):
    def test_revision_conflict_refuses_stale_overwrite(self):
        from research_state_store import (
            ResearchStateConflict,
            initialize_research_state,
            update_research_state,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "basket_state.json"
            initial = initialize_research_state(
                path,
                {"research_cohort_v2": {"runtime_enabled": True}},
            )
            current = update_research_state(
                path,
                initial["revision"],
                lambda payload: {
                    **payload,
                    "research_cohort_v2": {"runtime_enabled": False},
                },
            )
            with self.assertRaises(ResearchStateConflict):
                update_research_state(
                    path,
                    initial["revision"],
                    lambda payload: {
                        **payload,
                        "research_cohort_v2": {"runtime_enabled": True},
                    },
                )
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(initial["version"], 2)
        self.assertEqual(initial["revision"], 0)
        self.assertEqual(current["revision"], 1)
        self.assertFalse(persisted["research_cohort_v2"]["runtime_enabled"])
        self.assertEqual(persisted["revision"], 1)

    def test_two_processes_update_without_lost_update_or_partial_json(self):
        from research_state_store import initialize_research_state

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "basket_state.json"
            initialize_research_state(
                path,
                {"research_cohort_v2": {"runtime_enabled": True}},
            )
            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_increment_probe,
                    args=(str(path), start_event, result_queue),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start_event.set()
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            results = sorted(result_queue.get(timeout=5) for _ in processes)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(results, ["updated", "updated"])
        self.assertEqual(payload["research_cohort_v2"]["probe_updates"], 2)
        self.assertEqual(payload["revision"], 2)

    def test_atomic_replace_failure_preserves_previous_bytes(self):
        from research_state_store import (
            initialize_research_state,
            update_research_state,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "basket_state.json"
            state = initialize_research_state(
                path,
                {"research_cohort_v2": {"runtime_enabled": True}},
            )
            before = path.read_bytes()
            with patch("atomic_json_store.os.replace", side_effect=OSError("power loss")):
                with self.assertRaises(OSError):
                    update_research_state(
                        path,
                        state["revision"],
                        lambda payload: {
                            **payload,
                            "research_cohort_v2": {"runtime_enabled": False},
                        },
                    )
            after = path.read_bytes()

        self.assertEqual(before, after)
        self.assertTrue(json.loads(after)["research_cohort_v2"]["runtime_enabled"])

    def test_stale_collection_snapshot_cannot_reenable_operator_disabled_state(self):
        from research_state_store import (
            ResearchStateConflict,
            initialize_research_state,
            load_research_state,
            update_research_state,
        )
        from scripts.research_control import disable_research

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "basket_state.json"
            initialize_research_state(
                path,
                {
                    "research_cohort_v2": {
                        "runtime_enabled": True,
                        "probes": {"probe_1": {"valid_n": 1}},
                    }
                },
            )
            stale = load_research_state(path)
            disable_research(
                path,
                reason="operator pause",
                now="2026-08-28T09:00:00+08:00",
            )
            stale["research_cohort_v2"]["probes"]["probe_1"]["valid_n"] = 2
            with self.assertRaises(ResearchStateConflict):
                update_research_state(
                    path,
                    stale["revision"],
                    lambda _payload: stale,
                )
            persisted = load_research_state(path)

        self.assertFalse(persisted["research_cohort_v2"]["runtime_enabled"])
        self.assertEqual(
            persisted["research_cohort_v2"]["probes"]["probe_1"]["valid_n"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
