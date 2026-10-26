from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from harneloop.candidate import active_candidate_ids, create_candidate, list_candidates
from harneloop.preferences import list_registered_units, register_unit
from harneloop.runs import add_artifact, read_run, start_run
from harneloop.intake import acknowledge_intake
from harneloop.state import read_state
from harneloop.unit import init_unit


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_candidate_creation_preserves_every_open_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            unit = init_unit(Path(temp_dir) / "unit", "demo", "Demo Unit")
            summaries = [f"Independent change {index}" for index in range(12)]
            start = threading.Event()

            def worker(summary: str) -> str:
                start.wait(timeout=5)
                return create_candidate(unit, summary, plane="infrastructure").name

            with ThreadPoolExecutor(max_workers=len(summaries)) as executor:
                futures = [executor.submit(worker, summary) for summary in summaries]
                start.set()
                returned_ids = [future.result(timeout=10) for future in futures]

            candidate_ids = [str(record["id"]) for record in list_candidates(unit, include_closed=False)]
            self.assertEqual(len(candidate_ids), len(summaries))
            self.assertEqual(set(candidate_ids), set(returned_ids))
            self.assertEqual(set(read_state(unit)["active_candidates"]), set(candidate_ids))

    def test_candidate_list_is_not_overwritten_by_an_older_one(self) -> None:
        # The first creation pauses right after listing the open candidates. If that
        # listing happens outside the state lock, the second creation finishes during
        # the pause and the first one then writes its older list over it.
        with tempfile.TemporaryDirectory() as temp_dir:
            unit = init_unit(Path(temp_dir) / "unit", "demo", "Demo Unit")
            listed = threading.Event()
            second_created = threading.Event()

            def list_then_pause(unit_root: Path) -> list[str]:
                ids = active_candidate_ids(unit_root)
                if not listed.is_set():
                    listed.set()
                    second_created.wait(timeout=1)
                return ids

            with mock.patch("harneloop.candidate.active_candidate_ids", side_effect=list_then_pause):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    first = executor.submit(create_candidate, unit, "First change", plane="infrastructure")
                    self.assertTrue(listed.wait(timeout=5))
                    second = create_candidate(unit, "Second change", plane="infrastructure")
                    second_created.set()
                    first_id = first.result(timeout=10).name

            self.assertEqual(sorted(read_state(unit)["active_candidates"]), sorted([first_id, second.name]))

    def test_parallel_unit_registrations_do_not_lose_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "harneloop-home"
            units = [init_unit(root / f"unit-{index}", f"unit-{index}", f"Unit {index}") for index in range(12)]
            start = threading.Event()

            def worker(unit: Path) -> str:
                start.wait(timeout=5)
                return str(register_unit(home, unit)["id"])

            with ThreadPoolExecutor(max_workers=len(units)) as executor:
                futures = [executor.submit(worker, unit) for unit in units]
                start.set()
                returned_ids = [future.result(timeout=10) for future in futures]

            registered_ids = [str(record["id"]) for record in list_registered_units(home)]
            self.assertEqual(len(registered_ids), len(units))
            self.assertEqual(set(registered_ids), set(returned_ids))

    def test_parallel_artifact_adds_do_not_lose_run_yaml_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            harness_unit = init_unit(root / "unit", "demo", "Demo Unit")
            acknowledge_intake(harness_unit, basis="user_delegated", note="Test delegates setup context.")
            start_run(harness_unit, task="Capture artifacts concurrently")

            sources = []
            for index in range(12):
                source = root / f"artifact-{index}.txt"
                source.write_text(f"artifact {index}\n", encoding="utf-8")
                sources.append(source)

            start = threading.Event()

            def worker(source: Path) -> str:
                start.wait(timeout=5)
                record = add_artifact(
                    harness_unit,
                    "run-0001",
                    source,
                    kind="text",
                    description=source.name,
                )
                return str(record["id"])

            with ThreadPoolExecutor(max_workers=len(sources)) as executor:
                futures = [executor.submit(worker, source) for source in sources]
                start.set()
                returned_ids = [future.result(timeout=10) for future in futures]

            run_record = read_run(harness_unit, "run-0001")
            stored_artifacts = run_record["artifacts"]
            stored_ids = [artifact["id"] for artifact in stored_artifacts]

            self.assertEqual(len(stored_artifacts), len(sources))
            self.assertEqual(len(set(stored_ids)), len(sources))
            self.assertEqual(len(set(returned_ids)), len(sources))


if __name__ == "__main__":
    unittest.main()
