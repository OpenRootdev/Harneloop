from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .candidate import OPEN_CANDIDATE_STATUSES, candidate_digest, read_candidate
from .errors import HarneloopError
from .intake import ensure_intake_ready
from .locking import file_lock, harness_lock_path
from .state import now_iso, update_state
from .versioning import ensure_unit, hash_file
from .yamlio import read_yaml, write_yaml


VALID_RUN_STATUSES = {"running", "succeeded", "failed", "stopped"}


def runtime_root(unit_root: Path) -> Path:
    return unit_root / "runtime"


def runs_root(unit_root: Path) -> Path:
    return runtime_root(unit_root) / "runs"


def artifacts_root(unit_root: Path) -> Path:
    return runtime_root(unit_root) / "artifacts"


def next_run_id(unit_root: Path) -> str:
    root = runs_root(unit_root)
    root.mkdir(parents=True, exist_ok=True)
    existing: list[int] = []
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("run-"):
            try:
                existing.append(int(path.name.removeprefix("run-")))
            except ValueError:
                continue
    return f"run-{(max(existing, default=0) + 1):04d}"


def run_path(unit_root: Path, run_id: str) -> Path:
    path = runs_root(unit_root) / run_id
    if not path.exists():
        raise HarneloopError(f"Run does not exist: {run_id}")
    return path


def read_run(unit_root: Path, run_id: str) -> dict[str, Any]:
    return read_yaml(run_path(unit_root, run_id) / "run.yaml")


def write_run(unit_root: Path, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    write_yaml(run_path(unit_root, run_id) / "run.yaml", data)
    return data


def start_run(
    unit_root: Path,
    task: str,
    candidate_id: str | None = None,
    attempt_id: str | None = None,
    *,
    benchmark_id: str | None = None,
    benchmark_task_id: str | None = None,
    benchmark_trial: int | None = None,
    benchmark_arm: str | None = None,
) -> Path:
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    ensure_intake_ready(unit_root)
    candidate: dict[str, Any] | None = None
    candidate_sha256: str | None = None
    if candidate_id:
        candidate_sha256 = candidate_digest(unit_root, candidate_id)
        candidate = read_candidate(unit_root, candidate_id)
        status = str(candidate.get("status", "draft"))
        if status not in OPEN_CANDIDATE_STATUSES or status == "needs_rebase":
            raise HarneloopError(f"Candidate `{candidate_id}` cannot be tested while its status is `{status}`")
    benchmark_fields = (benchmark_id, benchmark_task_id, benchmark_trial, benchmark_arm)
    if any(value is not None for value in benchmark_fields):
        if (
            not all(value is not None for value in benchmark_fields)
            or not isinstance(benchmark_id, str)
            or not benchmark_id.strip()
            or not isinstance(benchmark_task_id, str)
            or not benchmark_task_id.strip()
            or isinstance(benchmark_trial, bool)
            or not isinstance(benchmark_trial, int)
            or benchmark_trial < 0
            or benchmark_arm not in {"baseline", "candidate"}
            or (benchmark_arm == "baseline" and candidate_id is not None)
            or (benchmark_arm == "candidate" and candidate_id is None)
        ):
            raise HarneloopError("Benchmark run identity is incomplete or inconsistent")
        from .benchmarks import read_benchmark

        benchmark = read_benchmark(unit_root, benchmark_id)
        task_record = next(
            (item for item in benchmark["tasks"] if item["task_id"] == benchmark_task_id),
            None,
        )
        binding = benchmark.get("candidate_binding")
        if (
            task_record is None
            or task_record.get("split") != "test"
            or benchmark_trial >= benchmark["repeats"]
            or not isinstance(binding, dict)
        ):
            raise HarneloopError("Benchmark run must match a bound heldout task and trial")
        if candidate_digest(unit_root, binding["candidate_id"]) != binding["candidate_sha256"]:
            raise HarneloopError("Candidate overlay changed after benchmark binding")
        if benchmark_arm == "candidate":
            if candidate_id != binding.get("candidate_id"):
                raise HarneloopError("Candidate run does not match the preregistered benchmark candidate")
        elif candidate_id is not None:
            raise HarneloopError("Baseline benchmark runs cannot use a candidate overlay")
    with file_lock(harness_lock_path(unit_root, "runs")):
        unit_meta = read_yaml(unit_root / "unit.yaml")
        run_id = next_run_id(unit_root)
        root = runs_root(unit_root) / run_id
        root.mkdir(parents=True, exist_ok=False)
        (artifacts_root(unit_root) / run_id).mkdir(parents=True, exist_ok=True)

        write_yaml(
            root / "run.yaml",
            {
                "schema_version": "0.1",
                "id": run_id,
                "task": task,
                "status": "running",
                "created_at": now_iso(),
                "finished_at": None,
                "harness_version": unit_meta.get("current_version"),
                "candidate_id": candidate_id,
                "candidate_base_version": candidate.get("base_version") if candidate else None,
                "candidate_sha256": candidate_sha256,
                "candidate_validation_tier": candidate.get("validation_tier") if candidate else None,
                "benchmark_id": benchmark_id,
                "benchmark_task_id": benchmark_task_id,
                "benchmark_trial": benchmark_trial,
                "benchmark_arm": benchmark_arm,
                "attempt_id": attempt_id,
                "evaluation_status": "pending",
                "evaluation_outcome": None,
                "summary": None,
                "artifacts": [],
            },
        )
    update_state(
        unit_root,
        state="active",
        active_run=run_id,
        reason="run_started",
        next_action=f"Collect artifacts and finish `{run_id}`.",
    )
    return root


def add_artifact(
    unit_root: Path,
    run_id: str,
    source_path: Path,
    kind: str,
    description: str = "",
    name: str | None = None,
) -> dict[str, Any]:
    unit_root = unit_root.resolve()
    source = source_path.resolve()
    if not source.is_file():
        raise HarneloopError(f"Artifact source is not a file: {source}")

    with file_lock(harness_lock_path(unit_root, f"run-{run_id}")):
        run_record = read_run(unit_root, run_id)
        if run_record.get("status") != "running":
            raise HarneloopError(f"Run is already finished and cannot accept artifacts: {run_id}")
        existing_artifacts = run_record.get("artifacts") or []
        artifact_id = f"artifact-{len(existing_artifacts) + 1:04d}"
        target_name = name or source.name
        target = artifacts_root(unit_root) / run_id / target_name
        if target.exists():
            target = artifacts_root(unit_root) / run_id / f"{artifact_id}-{target_name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

        record: dict[str, Any] = {
            "id": artifact_id,
            "kind": kind,
            "description": description,
            "source_path": str(source),
            "stored_path": target.relative_to(unit_root).as_posix(),
            "sha256": hash_file(target),
            "size": target.stat().st_size,
            "created_at": now_iso(),
        }
        run_record["artifacts"] = [*existing_artifacts, record]
        write_run(unit_root, run_id, run_record)
        return record


def finish_run(unit_root: Path, run_id: str, status: str, summary: str | None = None) -> dict[str, Any]:
    if status not in VALID_RUN_STATUSES - {"running"}:
        allowed = ", ".join(sorted(VALID_RUN_STATUSES - {"running"}))
        raise HarneloopError(f"Invalid final run status `{status}`. Expected one of: {allowed}")

    unit_root = unit_root.resolve()
    with file_lock(harness_lock_path(unit_root, f"run-{run_id}")):
        run_record = read_run(unit_root, run_id)
        if run_record.get("status") != "running":
            raise HarneloopError(f"Run is already finished and cannot be finished again: {run_id}")
        run_record["status"] = status
        run_record["summary"] = summary
        run_record["finished_at"] = now_iso()
        write_run(unit_root, run_id, run_record)
    update_state(
        unit_root,
        state="awaiting_evaluation",
        active_run=None,
        reason="run_execution_finished",
        next_action=(
            f"Evaluate the artifacts from `{run_id}` and conclude the linked attempt. "
            "Do not treat execution success as result quality."
        ),
    )
    return run_record
