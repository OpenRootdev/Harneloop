from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from harneloop.benchmarks import (
    bind_benchmark_candidate,
    compare_benchmark,
    create_benchmark,
    read_benchmark,
    record_benchmark_result,
)
from harneloop.candidate import create_candidate, set_candidate_status
from harneloop.errors import HarneloopError
from harneloop.intake import acknowledge_intake
from harneloop.runs import add_artifact, finish_run, read_run, start_run
from harneloop.unit import init_unit


class BenchmarkEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.unit = init_unit(root / "unit", "benchmark-unit", "Benchmark Unit")
        acknowledge_intake(self.unit, basis="user_delegated", note="Test delegates setup context.")
        self.candidate = create_candidate(self.unit, "Improve task solving", plane="target_harness")
        set_candidate_status(self.unit, self.candidate.name, "ready")
        self.manifest = {
            "source": {"name": "sample-eval", "revision": "sha256:" + "a" * 64},
            "tasks": [
                {"task_id": "dev-1", "split": "dev"},
                {"task_id": "test-1", "split": "test"},
                {"task_id": "test-2", "split": "test"},
            ],
            "repeats": 2,
            "seed": 17,
            "executor": "codex-subagent",
            "model": "luna-6",
            "effort": "medium",
            "settings": {"temperature": 0},
            "tools": {"filesystem": True, "terminal": True},
            "budget_policy": {"turn_cap": 20, "wall_seconds": 900},
            "grader": {"name": "official-evaluator", "revision": "b" * 40},
        }
        self.observed_conditions = {
            key: self.manifest[key]
            for key in ("executor", "model", "effort", "settings", "tools", "budget_policy")
        }
        create_benchmark(self.unit, "pilot", self.manifest)
        self.benchmark = bind_benchmark_candidate(self.unit, "pilot", self.candidate.name)

    def _finished_run(
        self,
        arm: str,
        label: str,
        content: str,
        *,
        task_id: str = "test-1",
        trial: int = 0,
        run_status: str = "succeeded",
    ) -> tuple[str, str]:
        candidate_id = self.candidate.name if arm == "candidate" else None
        run_root = start_run(
            self.unit,
            task=label,
            candidate_id=candidate_id,
            benchmark_id="pilot",
            benchmark_task_id=task_id,
            benchmark_trial=trial,
            benchmark_arm=arm,
        )
        run_id = run_root.name
        artifact = self.unit.parent / f"{run_id}-{arm}.patch"
        artifact.write_text(content, encoding="utf-8")
        artifact_record = add_artifact(self.unit, run_id, artifact, kind="patch")
        finish_run(self.unit, run_id, run_status, "Task attempt complete")
        return run_id, artifact_record["id"]

    def _record(
        self,
        arm: str,
        task_id: str,
        trial: int,
        score: int | float | None,
        *,
        status: str = "scored",
        reason: str | None = None,
    ) -> dict:
        run_id, artifact_id = self._finished_run(
            arm,
            f"{arm}:{task_id}:{trial}",
            f"{score}\n",
            task_id=task_id,
            trial=trial,
        )
        return record_benchmark_result(
            self.unit,
            "pilot",
            observed_conditions=self.observed_conditions,
            arm=arm,
            task_id=task_id,
            trial=trial,
            run_id=run_id,
            score=score,
            artifact_ids=[artifact_id],
            status=status,
            reason=reason,
        )

    def test_manifest_is_frozen_and_fingerprinted(self) -> None:
        created = read_benchmark(self.unit, "pilot")

        self.assertEqual(created["source"], self.manifest["source"])
        self.assertEqual(len(created["manifest_sha256"]), 64)
        self.assertEqual(len(created["conditions_sha256"]), 64)
        with self.assertRaises(HarneloopError):
            create_benchmark(self.unit, "pilot", self.manifest)

    def test_candidate_binding_is_once_only(self) -> None:
        with self.assertRaisesRegex(HarneloopError, "already bound"):
            bind_benchmark_candidate(self.unit, "pilot", self.candidate.name)

    def test_candidate_must_be_marked_ready_before_binding(self) -> None:
        other = create_candidate(self.unit, "Still refining", plane="target_harness")
        create_benchmark(self.unit, "not-ready", self.manifest)
        with self.assertRaisesRegex(HarneloopError, "Mark the dev-refined candidate ready"):
            bind_benchmark_candidate(self.unit, "not-ready", other.name)

    def test_manifest_cannot_self_attest_candidate_binding(self) -> None:
        manifest = {
            **self.manifest,
            "candidate_binding": {
                "candidate_id": self.candidate.name,
                "candidate_sha256": "0" * 64,
            },
        }
        with self.assertRaisesRegex(HarneloopError, "benchmark bind"):
            create_benchmark(self.unit, "self-bound", manifest)

    def test_heldout_run_cannot_start_before_candidate_binding(self) -> None:
        create_benchmark(self.unit, "unbound", self.manifest)
        with self.assertRaisesRegex(HarneloopError, "bound heldout task"):
            start_run(
                self.unit,
                task="unbound heldout",
                benchmark_id="unbound",
                benchmark_task_id="test-1",
                benchmark_trial=0,
                benchmark_arm="baseline",
            )

    def test_manifest_rejects_mutable_source_or_grader_revision(self) -> None:
        manifest = {**self.manifest, "source": {"name": "sample-eval", "revision": "main"}}
        with self.assertRaisesRegex(HarneloopError, "commit SHA"):
            create_benchmark(self.unit, "mutable-source", manifest)
        manifest = {**self.manifest, "grader": {"name": "grader", "revision": "latest"}}
        with self.assertRaisesRegex(HarneloopError, "commit SHA"):
            create_benchmark(self.unit, "mutable-grader", manifest)

    def test_candidate_overlay_change_after_binding_is_rejected(self) -> None:
        changes = self.unit / "candidates" / self.candidate.name / "changes"
        (changes / "solver.py").write_text("def solve(): return 1\n", encoding="utf-8")
        with self.assertRaisesRegex(HarneloopError, "Candidate overlay changed"):
            self._record("candidate", "test-1", 0, 1.0)

    def test_run_task_identity_must_match_recorded_cell(self) -> None:
        run_id, artifact_id = self._finished_run("baseline", "wrong task", "patch")
        with self.assertRaisesRegex(HarneloopError, "frozen task/trial identity"):
            record_benchmark_result(
                self.unit,
                "pilot",
                observed_conditions=self.observed_conditions,
                arm="baseline",
                task_id="test-2",
                trial=0,
                run_id=run_id,
                score=1.0,
                artifact_ids=[artifact_id],
            )

    def test_failed_run_cannot_be_claimed_as_scored(self) -> None:
        run_id, artifact_id = self._finished_run(
            "baseline", "failed task", "partial", run_status="failed"
        )
        with self.assertRaisesRegex(HarneloopError, "cannot be reported as scored"):
            record_benchmark_result(
                self.unit,
                "pilot",
                observed_conditions=self.observed_conditions,
                arm="baseline",
                task_id="test-1",
                trial=0,
                run_id=run_id,
                score=1.0,
                artifact_ids=[artifact_id],
            )

    def test_one_run_cannot_be_reused_for_another_result(self) -> None:
        row = self._record("baseline", "test-1", 0, 1.0)
        with self.assertRaisesRegex(HarneloopError, "only one benchmark task/trial row"):
            record_benchmark_result(
                self.unit,
                "pilot",
                observed_conditions=self.observed_conditions,
                arm="baseline",
                task_id="test-1",
                trial=0,
                run_id=row["run_id"],
                score=1.0,
                artifact_ids=[artifact["id"] for artifact in row["artifacts"]],
            )

    def test_symlinked_artifact_run_root_is_rejected(self) -> None:
        run_id, artifact_id = self._finished_run("baseline", "symlink test", "patch")
        run_artifacts = self.unit / "runtime" / "artifacts" / run_id
        original_is_symlink = Path.is_symlink

        def report_symlink(path: Path) -> bool:
            return path == run_artifacts or original_is_symlink(path)

        with patch.object(Path, "is_symlink", report_symlink):
            with self.assertRaisesRegex(HarneloopError, "symlinks"):
                record_benchmark_result(
                    self.unit,
                    "pilot",
                    observed_conditions=self.observed_conditions,
                    arm="baseline",
                    task_id="test-1",
                    trial=0,
                    run_id=run_id,
                    score=1.0,
                    artifact_ids=[artifact_id],
                )

    def test_symlinked_run_record_root_is_rejected(self) -> None:
        run_id, artifact_id = self._finished_run("baseline", "run path symlink", "patch")
        run_root = self.unit / "runtime" / "runs" / run_id
        original_is_symlink = Path.is_symlink

        def report_symlink(path: Path) -> bool:
            return path == run_root or original_is_symlink(path)

        with patch.object(Path, "is_symlink", report_symlink):
            with self.assertRaisesRegex(HarneloopError, "symlinks"):
                record_benchmark_result(
                    self.unit,
                    "pilot",
                    observed_conditions=self.observed_conditions,
                    arm="baseline",
                    task_id="test-1",
                    trial=0,
                    run_id=run_id,
                    score=1.0,
                    artifact_ids=[artifact_id],
                )

    def test_symlinked_benchmark_storage_root_is_rejected(self) -> None:
        benchmark_root = self.unit / ".evolve" / "benchmarks" / "pilot"
        original_is_symlink = Path.is_symlink

        def report_symlink(path: Path) -> bool:
            return path == benchmark_root or original_is_symlink(path)

        with patch.object(Path, "is_symlink", report_symlink):
            with self.assertRaisesRegex(HarneloopError, "symlinks"):
                read_benchmark(self.unit, "pilot")

    def test_comparison_pairs_exact_heldout_trials_and_counts_unavailable(self) -> None:
        for task_id in ("test-1", "test-2"):
            for trial in range(2):
                self._record("baseline", task_id, trial, 0.0 if trial == 0 else 1.0)
                if task_id == "test-2" and trial == 1:
                    self._record(
                        "candidate",
                        task_id,
                        trial,
                        None,
                        status="unavailable",
                        reason="executor timed out",
                    )
                else:
                    self._record("candidate", task_id, trial, 1.0)

        report = compare_benchmark(self.unit, "pilot", candidate_id=self.candidate.name)

        self.assertEqual(report["expected_pairs"], 4)
        self.assertEqual(report["arms"]["baseline"]["scored"], 4)
        self.assertEqual(report["arms"]["baseline"]["failed"], 2)
        self.assertEqual(report["arms"]["candidate"]["unavailable"], 1)
        self.assertEqual(report["paired"]["wins"], 2)
        self.assertEqual(report["paired"]["losses"], 0)
        self.assertEqual(report["paired"]["ties"], 1)
        self.assertEqual(report["paired"]["unavailable"], 1)
        self.assertIn("test-1", report["per_task"])

    def test_comparison_rejects_missing_task_trial_rows(self) -> None:
        self._record("baseline", "test-1", 0, 1.0)
        self._record("candidate", "test-1", 0, 1.0)

        with self.assertRaisesRegex(HarneloopError, "incomplete"):
            compare_benchmark(self.unit, "pilot", candidate_id=self.candidate.name)

    def test_result_rejects_nonfinite_score(self) -> None:
        run_id, artifact_id = self._finished_run("baseline", "invalid score", "patch")

        with self.assertRaisesRegex(HarneloopError, "finite numeric"):
            record_benchmark_result(
                self.unit,
                "pilot",
                observed_conditions=self.observed_conditions,
                arm="baseline",
                task_id="test-1",
                trial=0,
                run_id=run_id,
                score=float("nan"),
                artifact_ids=[artifact_id],
            )

    def test_result_rejects_out_of_range_score_and_condition_mismatch(self) -> None:
        run_id, artifact_id = self._finished_run("baseline", "invalid result", "patch")
        with self.assertRaisesRegex(HarneloopError, "between 0 and 1"):
            record_benchmark_result(
                self.unit,
                "pilot",
                observed_conditions=self.observed_conditions,
                arm="baseline",
                task_id="test-1",
                trial=0,
                run_id=run_id,
                score=1.1,
                artifact_ids=[artifact_id],
            )
        mismatched = {**self.observed_conditions, "model": "different-model"}
        with self.assertRaisesRegex(HarneloopError, "do not match"):
            record_benchmark_result(
                self.unit,
                "pilot",
                observed_conditions=mismatched,
                arm="baseline",
                task_id="test-1",
                trial=0,
                run_id=run_id,
                score=1.0,
                artifact_ids=[artifact_id],
            )

    def test_manifest_tampering_is_rejected(self) -> None:
        from harneloop.yamlio import read_yaml, write_yaml

        path = self.unit / ".evolve" / "benchmarks" / "pilot" / "manifest.yaml"
        manifest = read_yaml(path)
        manifest["model"] = "tampered"
        write_yaml(path, manifest)

        with self.assertRaisesRegex(HarneloopError, "manifest hash mismatch"):
            read_benchmark(self.unit, "pilot")

    def test_result_record_tampering_is_rejected(self) -> None:
        from harneloop.yamlio import read_yaml, write_yaml

        self._record("baseline", "test-1", 0, 1.0)
        result_root = self.unit / ".evolve" / "benchmarks" / "pilot" / "results"
        result_path = next(result_root.glob("*.yaml"))
        row = read_yaml(result_path)
        row["score"] = 0.0
        write_yaml(result_path, row)

        with self.assertRaisesRegex(HarneloopError, "record hash mismatch"):
            compare_benchmark(self.unit, "pilot", candidate_id=self.candidate.name)

    def test_result_rejects_invalid_optional_metrics(self) -> None:
        run_id, artifact_id = self._finished_run("baseline", "invalid metric", "patch")

        with self.assertRaisesRegex(HarneloopError, "integer token count"):
            record_benchmark_result(
                self.unit,
                "pilot",
                observed_conditions=self.observed_conditions,
                arm="baseline",
                task_id="test-1",
                trial=0,
                run_id=run_id,
                score=1.0,
                artifact_ids=[artifact_id],
                metrics={"input_tokens": 2.5},
            )

    def test_result_rejects_running_run_and_wrong_split(self) -> None:
        run_root = start_run(
            self.unit,
            task="still running",
            benchmark_id="pilot",
            benchmark_task_id="test-1",
            benchmark_trial=0,
            benchmark_arm="baseline",
        )
        artifact = self.unit.parent / "running.patch"
        artifact.write_text("patch\n", encoding="utf-8")
        artifact_record = add_artifact(self.unit, run_root.name, artifact, kind="patch")

        with self.assertRaisesRegex(HarneloopError, "finished"):
            record_benchmark_result(
                self.unit,
                "pilot",
                observed_conditions=self.observed_conditions,
                arm="baseline",
                task_id="test-1",
                trial=0,
                run_id=run_root.name,
                score=1.0,
                artifact_ids=[artifact_record["id"]],
            )

        finish_run(self.unit, run_root.name, "succeeded")
        with self.assertRaisesRegex(HarneloopError, "test split"):
            record_benchmark_result(
                self.unit,
                "pilot",
                observed_conditions=self.observed_conditions,
                arm="baseline",
                task_id="dev-1",
                trial=0,
                run_id=run_root.name,
                score=1.0,
                artifact_ids=[artifact_record["id"]],
            )

    def test_comparison_detects_mutated_artifact(self) -> None:
        baseline_run = None
        for task_id in ("test-1", "test-2"):
            for trial in range(2):
                for arm in ("baseline", "candidate"):
                    run_id, artifact_id = self._finished_run(
                        arm, f"{arm}:{task_id}:{trial}", "patch", task_id=task_id, trial=trial
                    )
                    record_benchmark_result(
                        self.unit,
                        "pilot",
                        observed_conditions=self.observed_conditions,
                        arm=arm,
                        task_id=task_id,
                        trial=trial,
                        run_id=run_id,
                        score=1.0,
                        artifact_ids=[artifact_id],
                    )
                    if arm == "baseline" and task_id == "test-1" and trial == 0:
                        baseline_run = run_id
        baseline_record = read_run(self.unit, baseline_run)
        artifact_path = self.unit / baseline_record["artifacts"][0]["stored_path"]
        artifact_path.write_text("tampered", encoding="utf-8")

        with self.assertRaisesRegex(HarneloopError, "hash mismatch"):
            compare_benchmark(self.unit, "pilot", candidate_id=self.candidate.name)

    def test_duplicate_result_key_is_rejected(self) -> None:
        self._record("baseline", "test-1", 0, 1.0)
        with self.assertRaisesRegex(HarneloopError, "already exists"):
            self._record("baseline", "test-1", 0, 1.0)

    def test_metrics_are_only_reported_when_available(self) -> None:
        run_id, artifact_id = self._finished_run("baseline", "metrics", "patch")
        row = record_benchmark_result(
            self.unit,
            "pilot",
            observed_conditions=self.observed_conditions,
            arm="baseline",
            task_id="test-1",
            trial=0,
            run_id=run_id,
            score=1.0,
            artifact_ids=[artifact_id],
            metrics={
                "duration_seconds": 12.5,
                "input_tokens": 800,
                "output_tokens": 400,
                "cost_usd": 0.0,
            },
        )
        for task_id in ("test-1", "test-2"):
            for trial in range(2):
                if task_id == "test-1" and trial == 0:
                    self._record("candidate", task_id, trial, 1.0)
                    continue
                self._record("baseline", task_id, trial, 1.0)
                self._record("candidate", task_id, trial, 1.0)

        report = compare_benchmark(self.unit, "pilot", candidate_id=self.candidate.name)

        self.assertEqual(row["metrics"]["duration_seconds"], 12.5)
        self.assertEqual(row["metrics"]["input_tokens"], 800)
        self.assertEqual(row["metrics"]["output_tokens"], 400)
        self.assertEqual(row["metrics"]["cost_usd"], 0.0)
        self.assertEqual(len(report["metric_observations"]["duration_seconds"]), 1)
        self.assertEqual(report["metric_observations"]["duration_seconds"][0]["value"], 12.5)


if __name__ == "__main__":
    unittest.main()
