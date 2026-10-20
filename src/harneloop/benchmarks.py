from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .candidate import candidate_digest
from .errors import HarneloopError
from .locking import file_lock, harness_lock_path
from .runs import VALID_RUN_STATUSES
from .versioning import ensure_unit, hash_file
from .yamlio import read_yaml, write_yaml


_BENCHMARK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IMMUTABLE_REVISION = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|sha256:[0-9a-fA-F]{64})$")
_RUN_ID = re.compile(r"^run-[A-Za-z0-9._-]{1,64}$")
_CONDITION_FIELDS = ("executor", "model", "effort", "settings", "tools", "budget_policy")
_METRIC_FIELDS = ("duration_seconds", "input_tokens", "output_tokens", "cost_usd")


def _benchmark_dir(unit_root: Path, benchmark_id: str) -> Path:
    if not _BENCHMARK_ID.fullmatch(benchmark_id) or benchmark_id in {".", ".."}:
        raise HarneloopError(f"Invalid benchmark ID: {benchmark_id!r}")
    return unit_root / ".evolve" / "benchmarks" / benchmark_id


def _safe_unit_path(unit_root: Path, path: Path, label: str) -> Path:
    """Resolve a unit path while rejecting symlinks and escapes at every component."""
    unit_root = unit_root.resolve()
    try:
        relative = path.relative_to(unit_root)
    except ValueError:
        raise HarneloopError(f"{label} path is outside the harness unit") from None
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise HarneloopError(f"{label} path contains an unsafe component")
    cursor = unit_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise HarneloopError(f"{label} path cannot contain symlinks: {cursor}")
    resolved = path.resolve()
    if not resolved.is_relative_to(unit_root):
        raise HarneloopError(f"{label} path escapes the harness unit")
    return resolved


def _validate_immutable_revision(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _IMMUTABLE_REVISION.fullmatch(value):
        raise HarneloopError(
            f"{label} revision must be a 40/64-character commit SHA or sha256:<64 hex> checksum"
        )


def _json_safe(value: Any, path: str = "value") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HarneloopError(f"{path} must contain only finite numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_safe(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise HarneloopError(f"{path} mapping keys must be strings")
            _json_safe(item, f"{path}.{key}")
        return
    raise HarneloopError(f"{path} contains unsupported value type: {type(value).__name__}")


def _canonical_hash(data: dict[str, Any]) -> str:
    _json_safe(data)
    payload = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _conditions_from(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: manifest[key] for key in _CONDITION_FIELDS}


def _validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise HarneloopError("Benchmark manifest must be a mapping")
    required = {
        "source",
        "tasks",
        "repeats",
        "seed",
        *_CONDITION_FIELDS,
        "grader",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        raise HarneloopError(f"Benchmark manifest is missing required fields: {', '.join(missing)}")
    _json_safe(manifest, "manifest")

    source = manifest["source"]
    if (
        not isinstance(source, dict)
        or not _nonempty_text(source.get("name"))
        or not _nonempty_text(source.get("revision"))
    ):
        raise HarneloopError("Benchmark source requires a nonempty name and immutable revision")
    _validate_immutable_revision(source["revision"], "Benchmark source")
    grader = manifest["grader"]
    if (
        not isinstance(grader, dict)
        or not _nonempty_text(grader.get("name"))
        or not _nonempty_text(grader.get("revision"))
    ):
        raise HarneloopError("Benchmark grader requires a nonempty name and immutable revision")
    _validate_immutable_revision(grader["revision"], "Benchmark grader")
    repeats = manifest["repeats"]
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise HarneloopError("Benchmark repeats must be a positive integer")
    if manifest["seed"] is None or isinstance(manifest["seed"], (dict, list)):
        raise HarneloopError("Benchmark seed must be a scalar value")
    tasks = manifest["tasks"]
    if not isinstance(tasks, list) or not tasks:
        raise HarneloopError("Benchmark tasks must be a nonempty list")
    seen: set[str] = set()
    splits: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or not _nonempty_text(task.get("task_id")):
            raise HarneloopError("Each benchmark task requires a nonempty task_id")
        task_id = task["task_id"]
        if task_id in seen:
            raise HarneloopError(f"Duplicate benchmark task ID: {task_id}")
        seen.add(task_id)
        split = task.get("split")
        if split not in {"dev", "test"}:
            raise HarneloopError(f"Benchmark task `{task_id}` split must be `dev` or `test`")
        splits.add(split)
    if splits != {"dev", "test"}:
        raise HarneloopError("Benchmark manifest must include both dev and test tasks")

    for field in ("executor", "model", "effort"):
        if not _nonempty_text(manifest[field]):
            raise HarneloopError(f"Benchmark {field} must be nonempty")
    for field in ("settings", "tools", "budget_policy"):
        if not isinstance(manifest[field], (dict, list)):
            raise HarneloopError(f"Benchmark {field} must be a mapping or list")
    if not manifest["budget_policy"]:
        raise HarneloopError("Benchmark budget_policy must describe the fixed execution budget")
    binding = manifest.get("candidate_binding")
    if binding is not None and (
        not isinstance(binding, dict)
        or set(binding) != {"candidate_id", "candidate_sha256"}
        or not _nonempty_text(binding.get("candidate_id"))
        or not isinstance(binding.get("candidate_sha256"), str)
        or not re.fullmatch(r"[a-fA-F0-9]{64}", binding["candidate_sha256"])
    ):
        raise HarneloopError("Benchmark candidate_binding is invalid")
    # Store a detached JSON-compatible copy so later caller mutation cannot alter the frozen record.
    return json.loads(json.dumps(manifest, ensure_ascii=False, allow_nan=False))


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def create_benchmark(
    unit_root: Path, benchmark_id: str, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Freeze a benchmark design and its acting-condition fingerprints.

    Fingerprints provide integrity/provenance checks; they are not signatures or proof
    that a remote model actually used the attested executor and settings.
    """
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    root = _safe_unit_path(unit_root, _benchmark_dir(unit_root, benchmark_id), "Benchmark")
    if isinstance(manifest, dict) and "candidate_binding" in manifest:
        raise HarneloopError("Candidate binding must be created with benchmark bind after dev refinement")
    frozen = _validate_manifest(manifest)
    record = {
        "schema_version": "0.1",
        "id": benchmark_id,
        **frozen,
        "manifest_sha256": _canonical_hash(frozen),
        "conditions_sha256": _canonical_hash(_conditions_from(frozen)),
    }
    with file_lock(harness_lock_path(unit_root, f"benchmark-{benchmark_id}")):
        if root.exists():
            raise HarneloopError(f"Benchmark manifest is already frozen: {benchmark_id}")
        root.mkdir(parents=True, exist_ok=False)
        write_yaml(root / "manifest.yaml", record)
    return record


def read_benchmark(unit_root: Path, benchmark_id: str) -> dict[str, Any]:
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    root = _safe_unit_path(unit_root, _benchmark_dir(unit_root, benchmark_id), "Benchmark")
    path = _safe_unit_path(unit_root, root / "manifest.yaml", "Benchmark manifest")
    if not path.is_file():
        raise HarneloopError(f"Benchmark does not exist: {benchmark_id}")
    record = read_yaml(path)
    if record.get("id") != benchmark_id:
        raise HarneloopError(f"Frozen benchmark ID mismatch: {benchmark_id}")
    body = {
        key: value
        for key, value in record.items()
        if key not in {"manifest_sha256", "conditions_sha256"}
    }
    body.pop("schema_version", None)
    body.pop("id", None)
    if record.get("manifest_sha256") != _canonical_hash(body):
        raise HarneloopError(f"Frozen benchmark manifest hash mismatch: {benchmark_id}")
    _validate_manifest(body)
    if record.get("conditions_sha256") != _canonical_hash(_conditions_from(body)):
        raise HarneloopError(f"Frozen benchmark conditions hash mismatch: {benchmark_id}")
    return record


def bind_benchmark_candidate(
    unit_root: Path, benchmark_id: str, candidate_id: str
) -> dict[str, Any]:
    """Freeze one candidate identity before any heldout result is recorded."""
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    root = _safe_unit_path(unit_root, _benchmark_dir(unit_root, benchmark_id), "Benchmark")
    with file_lock(harness_lock_path(unit_root, f"benchmark-{benchmark_id}")):
        record = read_benchmark(unit_root, benchmark_id)
        if record.get("candidate_binding") is not None:
            raise HarneloopError(f"Benchmark candidate is already bound: {benchmark_id}")
        results = _safe_unit_path(unit_root, _result_dir(root), "Benchmark results")
        if results.exists() and any(results.iterdir()):
            raise HarneloopError("Candidate must be bound before any heldout benchmark rows are recorded")
        digest = candidate_digest(unit_root, candidate_id)
        candidate = read_yaml(unit_root / "candidates" / candidate_id / "candidate.yaml")
        if candidate.get("id") != candidate_id:
            raise HarneloopError(f"Candidate identity mismatch: {candidate_id}")
        if candidate.get("status") not in {"ready", "validating"}:
            raise HarneloopError("Mark the dev-refined candidate ready before benchmark binding")
        record["candidate_binding"] = {
            "candidate_id": candidate_id,
            "candidate_sha256": digest,
        }
        body = {
            key: value
            for key, value in record.items()
            if key not in {"manifest_sha256", "conditions_sha256", "schema_version", "id"}
        }
        record["manifest_sha256"] = _canonical_hash(body)
        path = _safe_unit_path(unit_root, root / "manifest.yaml", "Benchmark manifest")
        write_yaml(path, record)
        return record


def _result_dir(root: Path) -> Path:
    return root / "results"


def _read_safe_run(unit_root: Path, run_id: str) -> dict[str, Any]:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise HarneloopError(f"Invalid Harneloop run ID: {run_id!r}")
    path = _safe_unit_path(
        unit_root,
        unit_root / "runtime" / "runs" / run_id / "run.yaml",
        "Harneloop run record",
    )
    if not path.is_file():
        raise HarneloopError(f"Run does not exist: {run_id}")
    return read_yaml(path)


def _row_key(arm: str, task_id: str, trial: int) -> str:
    return _canonical_hash({"arm": arm, "task_id": task_id, "trial": trial})


def _validate_finite_score(score: Any) -> float:
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        raise HarneloopError("Benchmark score must be a finite numeric value from 0 to 1")
    if not 0 <= score <= 1:
        raise HarneloopError("Benchmark score must be between 0 and 1 inclusive")
    return float(score)


def _validate_metrics(metrics: dict[str, Any] | None) -> dict[str, int | float]:
    if metrics is None:
        return {}
    if not isinstance(metrics, dict):
        raise HarneloopError("Benchmark metrics must be a mapping")
    unknown = sorted(set(metrics) - set(_METRIC_FIELDS))
    if unknown:
        raise HarneloopError(f"Unsupported benchmark metrics: {', '.join(unknown)}")
    normalized: dict[str, int | float] = {}
    for key, value in metrics.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise HarneloopError(f"Benchmark metric `{key}` must be finite and nonnegative")
        if key in {"input_tokens", "output_tokens"} and not isinstance(value, int):
            raise HarneloopError(f"Benchmark metric `{key}` must be an integer token count")
        normalized[key] = value
    return normalized


def _artifact_records(
    unit_root: Path,
    run_id: str,
    run: dict[str, Any],
    artifact_ids: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise HarneloopError(f"Invalid Harneloop run ID: {run_id!r}")
    if (
        not isinstance(artifact_ids, list)
        or not artifact_ids
        or any(not _nonempty_text(item) for item in artifact_ids)
        or len(set(artifact_ids)) != len(artifact_ids)
    ):
        raise HarneloopError("Benchmark result must reference one or more unique stored artifacts")
    artifacts = run.get("artifacts") or []
    result: list[dict[str, Any]] = []
    for artifact_id in artifact_ids:
        artifact = next((item for item in artifacts if item.get("id") == artifact_id), None)
        if artifact is None:
            raise HarneloopError(f"Artifact `{artifact_id}` is not attached to run `{run_id}`")
        stored_path = artifact.get("stored_path")
        if not _nonempty_text(stored_path):
            raise HarneloopError(f"Artifact `{artifact_id}` has no stored path")
        stored = Path(stored_path)
        if stored.is_absolute() or ".." in stored.parts:
            raise HarneloopError(f"Artifact `{artifact_id}` has an unsafe stored path")
        path = _safe_unit_path(unit_root, unit_root / stored, "Run artifact")
        expected_root = _safe_unit_path(
            unit_root, unit_root / "runtime" / "artifacts" / run_id, "Run artifact directory"
        )
        if not path.is_relative_to(expected_root) or not path.is_file():
            raise HarneloopError(
                f"Artifact `{artifact_id}` is missing or outside its run artifact directory"
            )
        actual_hash = hash_file(path)
        if artifact.get("sha256") != actual_hash:
            raise HarneloopError(f"Artifact `{artifact_id}` hash mismatch")
        result.append(
            {
                "id": artifact_id,
                "stored_path": path.relative_to(unit_root).as_posix(),
                "sha256": actual_hash,
            }
        )
    return result


def record_benchmark_result(
    unit_root: Path,
    benchmark_id: str,
    *,
    observed_conditions: dict[str, Any],
    arm: str,
    task_id: str,
    trial: int,
    run_id: str,
    score: int | float | None,
    artifact_ids: list[str],
    status: str = "scored",
    reason: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record an externally graded row tied to a finished Harneloop run.

    `observed_conditions` is operator-attested provenance, not cryptographic proof
    of a remote model's identity or behavior.
    """
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    root = _benchmark_dir(unit_root, benchmark_id)
    benchmark = read_benchmark(unit_root, benchmark_id)
    if arm not in {"baseline", "candidate"}:
        raise HarneloopError("Benchmark result arm must be `baseline` or `candidate`")
    if not _nonempty_text(task_id):
        raise HarneloopError("Benchmark task_id must be nonempty")
    repeats = benchmark["repeats"]
    if isinstance(trial, bool) or not isinstance(trial, int) or not 0 <= trial < repeats:
        raise HarneloopError(f"Benchmark trial must be an integer from 0 to {repeats - 1}")
    task = next((item for item in benchmark["tasks"] if item["task_id"] == task_id), None)
    if task is None or task["split"] != "test":
        raise HarneloopError(
            f"Benchmark results can only be recorded for a frozen test split task: {task_id}"
        )
    binding = benchmark.get("candidate_binding")
    if not isinstance(binding, dict):
        raise HarneloopError("Bind the candidate before recording any heldout benchmark rows")
    if status not in {"scored", "failed", "unavailable"}:
        raise HarneloopError("Benchmark result status must be `scored`, `failed`, or `unavailable`")
    if status == "scored":
        normalized_score: float | None = _validate_finite_score(score)
        if reason:
            raise HarneloopError("A scored benchmark row cannot include an unavailable reason")
    elif status == "failed":
        normalized_score = _validate_finite_score(score)
        if normalized_score != 0:
            raise HarneloopError("A failed benchmark row must have score 0")
        if not _nonempty_text(reason):
            raise HarneloopError("Failed benchmark rows require a concrete failure reason")
    else:
        if score is not None or not _nonempty_text(reason):
            raise HarneloopError("Unavailable rows require a null score and a concrete reason")
        normalized_score = None

    if (
        not isinstance(observed_conditions, dict)
        or set(observed_conditions) != set(_CONDITION_FIELDS)
    ):
        raise HarneloopError(
            f"Observed conditions must contain exactly: {', '.join(_CONDITION_FIELDS)}"
        )
    actual_conditions_hash = _canonical_hash(observed_conditions)
    if actual_conditions_hash != benchmark["conditions_sha256"]:
        raise HarneloopError(
            "Observed conditions do not match the frozen benchmark settings and budget policy"
        )
    run = _read_safe_run(unit_root, run_id)
    if (
        run.get("benchmark_id") != benchmark_id
        or run.get("benchmark_task_id") != task_id
        or run.get("benchmark_trial") != trial
        or run.get("benchmark_arm") != arm
    ):
        raise HarneloopError("Benchmark result does not match the run's frozen task/trial identity")
    if run.get("status") not in VALID_RUN_STATUSES - {"running"}:
        raise HarneloopError(f"Benchmark result requires a finished Harneloop run: {run_id}")
    if run.get("status") != "succeeded" and status == "scored":
        raise HarneloopError("A failed or stopped Harneloop run cannot be reported as scored")
    if arm == "baseline" and run.get("candidate_id") is not None:
        raise HarneloopError("Baseline benchmark rows must reference a run without a candidate")
    if arm == "candidate":
        if run.get("candidate_id") != binding["candidate_id"]:
            raise HarneloopError("Candidate benchmark run does not match the preregistered candidate")
        current_digest = candidate_digest(unit_root, binding["candidate_id"])
        if run.get("candidate_sha256") != binding["candidate_sha256"] or current_digest != binding["candidate_sha256"]:
            raise HarneloopError("Candidate overlay changed after benchmark binding or run start")
    artifacts = _artifact_records(unit_root, run_id, run, artifact_ids)
    normalized_metrics = _validate_metrics(metrics)

    key_hash = _row_key(arm, task_id, trial)
    row = {
        "schema_version": "0.1",
        "benchmark_id": benchmark_id,
        "manifest_sha256": benchmark["manifest_sha256"],
        "conditions_sha256": benchmark["conditions_sha256"],
        "arm": arm,
        "task_id": task_id,
        "trial": trial,
        "run_id": run_id,
        "candidate_id": run.get("candidate_id"),
        "candidate_sha256": run.get("candidate_sha256"),
        "harness_version": run.get("harness_version"),
        "candidate_base_version": run.get("candidate_base_version"),
        "status": status,
        "score": normalized_score,
        "reason": reason.strip() if isinstance(reason, str) and reason.strip() else None,
        "observed_conditions": observed_conditions,
        "artifacts": artifacts,
        "metrics": normalized_metrics,
    }
    row["record_sha256"] = _canonical_hash(row)
    destination = _result_dir(root) / f"{arm}-{key_hash}.yaml"
    with file_lock(harness_lock_path(unit_root, f"benchmark-{benchmark_id}")):
        # Re-read under the shared benchmark lock so concurrent writers cannot replace a row.
        latest = read_benchmark(unit_root, benchmark_id)
        if latest.get("manifest_sha256") != benchmark["manifest_sha256"]:
            raise HarneloopError("Benchmark manifest changed while recording a result")
        results_root = _safe_unit_path(unit_root, _result_dir(root), "Benchmark results")
        if results_root.exists():
            existing = _read_rows(unit_root, root, latest)
            if any(item.get("run_id") == run_id for item in existing.values()):
                raise HarneloopError("A Harneloop run can back only one benchmark task/trial row")
        if destination.exists():
            raise HarneloopError(
                f"Benchmark result already exists for {arm}/{task_id}/trial-{trial}"
            )
        _safe_unit_path(unit_root, destination, "Benchmark result")
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_yaml(destination, row)
    return row


def _read_rows(
    unit_root: Path, root: Path, benchmark: dict[str, Any]
) -> dict[tuple[str, str, int], dict[str, Any]]:
    rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    results_root = _safe_unit_path(unit_root, _result_dir(root), "Benchmark results")
    for path in sorted(results_root.glob("*.yaml")):
        _safe_unit_path(unit_root, path, "Benchmark result")
        row = read_yaml(path)
        record_hash = row.get("record_sha256")
        body = {key: value for key, value in row.items() if key != "record_sha256"}
        if record_hash != _canonical_hash(body):
            raise HarneloopError(f"Benchmark result record hash mismatch: {path.name}")
        if (
            row.get("benchmark_id") != benchmark["id"]
            or row.get("manifest_sha256") != benchmark["manifest_sha256"]
        ):
            raise HarneloopError(
                f"Benchmark result references a different frozen manifest: {path.name}"
            )
        if row.get("conditions_sha256") != benchmark["conditions_sha256"]:
            raise HarneloopError(f"Benchmark result conditions hash mismatch: {path.name}")
        if _canonical_hash(row.get("observed_conditions")) != benchmark["conditions_sha256"]:
            raise HarneloopError(f"Benchmark result observed conditions mismatch: {path.name}")
        arm = row.get("arm")
        task_id = row.get("task_id")
        trial = row.get("trial")
        if arm not in {"baseline", "candidate"} or not _nonempty_text(task_id):
            raise HarneloopError(f"Invalid benchmark result identity: {path.name}")
        if (
            isinstance(trial, bool)
            or not isinstance(trial, int)
            or not 0 <= trial < benchmark["repeats"]
        ):
            raise HarneloopError(f"Invalid benchmark result trial: {path.name}")
        if row.get("status") == "scored":
            _validate_finite_score(row.get("score"))
            if row.get("reason"):
                raise HarneloopError(
                    f"Scored benchmark result has an unavailable reason: {path.name}"
                )
        elif row.get("status") == "failed":
            if (
                _validate_finite_score(row.get("score")) != 0
                or not _nonempty_text(row.get("reason"))
            ):
                raise HarneloopError(f"Invalid failed benchmark result: {path.name}")
        elif row.get("status") == "unavailable":
            if row.get("score") is not None or not _nonempty_text(row.get("reason")):
                raise HarneloopError(f"Invalid unavailable benchmark result: {path.name}")
        else:
            raise HarneloopError(f"Invalid benchmark result status: {path.name}")
        _validate_metrics(row.get("metrics") or {})
        key = (arm, task_id, trial)
        if key in rows:
            raise HarneloopError(f"Duplicate benchmark result key: {key}")
        if any(existing.get("run_id") == row.get("run_id") for existing in rows.values()):
            raise HarneloopError("A Harneloop run can back only one benchmark task/trial row")
        run = _read_safe_run(unit_root, row.get("run_id"))
        if run.get("status") not in VALID_RUN_STATUSES - {"running"}:
            raise HarneloopError(f"Benchmark result references an unfinished run: {row['run_id']}")
        if run.get("status") != "succeeded" and row.get("status") == "scored":
            raise HarneloopError(f"Failed or stopped run is marked scored: {row['run_id']}")
        if run.get("candidate_id") != row.get("candidate_id"):
            raise HarneloopError(
                f"Benchmark result candidate linkage changed for run {row['run_id']}"
            )
        if run.get("candidate_sha256") != row.get("candidate_sha256"):
            raise HarneloopError(f"Benchmark run candidate digest changed for run {row['run_id']}")
        if (
            run.get("harness_version") != row.get("harness_version")
            or run.get("candidate_base_version") != row.get("candidate_base_version")
        ):
            raise HarneloopError(f"Benchmark run version linkage changed for run {row['run_id']}")
        if (
            run.get("benchmark_id") != benchmark["id"]
            or run.get("benchmark_task_id") != task_id
            or run.get("benchmark_trial") != trial
            or run.get("benchmark_arm") != arm
        ):
            raise HarneloopError(f"Benchmark run task/trial linkage changed for run {row['run_id']}")
        if arm == "candidate":
            binding = benchmark.get("candidate_binding")
            if (
                not isinstance(binding, dict)
                or run.get("candidate_id") != binding["candidate_id"]
                or run.get("candidate_sha256") != binding["candidate_sha256"]
                or candidate_digest(unit_root, binding["candidate_id"]) != binding["candidate_sha256"]
            ):
                raise HarneloopError(f"Candidate content no longer matches benchmark binding: {row['run_id']}")
        artifacts = row.get("artifacts")
        if not isinstance(artifacts, list) or any(not isinstance(item, dict) for item in artifacts):
            raise HarneloopError(f"Invalid benchmark artifact references: {path.name}")
        artifact_ids = [item.get("id") for item in artifacts]
        checked_artifacts = _artifact_records(unit_root, row["run_id"], run, artifact_ids)
        if checked_artifacts != artifacts:
            raise HarneloopError(
                f"Benchmark evidence artifact linkage changed for run {row['run_id']}"
            )
        rows[key] = row
    return rows


def compare_benchmark(unit_root: Path, benchmark_id: str, *, candidate_id: str) -> dict[str, Any]:
    """Summarize paired heldout scores; this does not run a provider or grader."""
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    root = _benchmark_dir(unit_root, benchmark_id)
    benchmark = read_benchmark(unit_root, benchmark_id)
    if not _nonempty_text(candidate_id):
        raise HarneloopError("Candidate ID must be nonempty")
    binding = benchmark.get("candidate_binding")
    if not isinstance(binding, dict) or binding.get("candidate_id") != candidate_id:
        raise HarneloopError("Comparison candidate does not match the preregistered benchmark binding")
    if candidate_digest(unit_root, candidate_id) != binding.get("candidate_sha256"):
        raise HarneloopError("Candidate overlay changed after benchmark binding")
    tasks = [item["task_id"] for item in benchmark["tasks"] if item["split"] == "test"]
    expected = {
        (arm, task_id, trial)
        for arm in ("baseline", "candidate")
        for task_id in tasks
        for trial in range(benchmark["repeats"])
    }
    with file_lock(harness_lock_path(unit_root, f"benchmark-{benchmark_id}")):
        benchmark = read_benchmark(unit_root, benchmark_id)
        rows = _read_rows(unit_root, root, benchmark)
    unexpected = set(rows) - expected
    if unexpected:
        raise HarneloopError(
            "Benchmark contains results outside the frozen test design: "
            f"{sorted(unexpected)}"
        )
    missing = expected - set(rows)
    if missing:
        raise HarneloopError(
            f"Benchmark result set is incomplete; missing {len(missing)} arm/task/trial rows"
        )
    for key, row in rows.items():
        arm = key[0]
        if arm == "baseline" and row.get("candidate_id") is not None:
            raise HarneloopError("Baseline benchmark row references a candidate-linked run")
        if arm == "candidate" and row.get("candidate_id") != candidate_id:
            raise HarneloopError("Candidate benchmark row references a different candidate")
    baseline_versions = {
        rows[("baseline", task_id, trial)].get("harness_version")
        for task_id in tasks
        for trial in range(benchmark["repeats"])
    }
    candidate_base_versions = {
        rows[("candidate", task_id, trial)].get("candidate_base_version")
        for task_id in tasks
        for trial in range(benchmark["repeats"])
    }
    if len(baseline_versions) != 1 or candidate_base_versions != baseline_versions:
        raise HarneloopError(
            "Baseline runs and candidate runs do not share the same harness base version"
        )

    expected_pairs = len(tasks) * benchmark["repeats"]
    arms: dict[str, dict[str, int]] = {}
    for arm in ("baseline", "candidate"):
        arm_rows = [row for (row_arm, _, _), row in rows.items() if row_arm == arm]
        unavailable = sum(row["status"] == "unavailable" for row in arm_rows)
        failed = sum(row["status"] == "failed" or row.get("score") == 0 for row in arm_rows)
        arms[arm] = {
            "expected": expected_pairs,
            "scored": len(arm_rows) - unavailable,
            "failed": failed,
            "unavailable": unavailable,
        }

    paired = {"wins": 0, "losses": 0, "ties": 0, "scored": 0, "unavailable": 0}
    per_task: dict[str, dict[str, Any]] = {}
    deltas: list[dict[str, Any]] = []
    for task_id in tasks:
        task_result = {"wins": 0, "losses": 0, "ties": 0, "unavailable": 0, "trials": []}
        for trial in range(benchmark["repeats"]):
            baseline = rows[("baseline", task_id, trial)]
            candidate = rows[("candidate", task_id, trial)]
            if baseline["status"] == "unavailable" or candidate["status"] == "unavailable":
                paired["unavailable"] += 1
                task_result["unavailable"] += 1
                task_result["trials"].append({"trial": trial, "outcome": "unavailable"})
                continue
            delta = candidate["score"] - baseline["score"]
            paired["scored"] += 1
            if delta > 0:
                outcome = "win"
                paired["wins"] += 1
                task_result["wins"] += 1
            elif delta < 0:
                outcome = "loss"
                paired["losses"] += 1
                task_result["losses"] += 1
            else:
                outcome = "tie"
                paired["ties"] += 1
                task_result["ties"] += 1
            delta_row = {
                "task_id": task_id,
                "trial": trial,
                "baseline_score": baseline["score"],
                "candidate_score": candidate["score"],
                "delta": delta,
            }
            deltas.append(delta_row)
            task_result["trials"].append({"trial": trial, "outcome": outcome, **delta_row})
        per_task[task_id] = task_result

    metric_observations: dict[str, list[dict[str, Any]]] = {field: [] for field in _METRIC_FIELDS}
    for (arm, task_id, trial), row in sorted(rows.items()):
        for field, value in (row.get("metrics") or {}).items():
            metric_observations[field].append(
                {"arm": arm, "task_id": task_id, "trial": trial, "value": value}
            )
    metric_observations = {field: values for field, values in metric_observations.items() if values}

    return {
        "benchmark_id": benchmark_id,
        "manifest_sha256": benchmark["manifest_sha256"],
        "conditions_sha256": benchmark["conditions_sha256"],
        "candidate_id": candidate_id,
        "candidate_sha256": binding["candidate_sha256"],
        "heldout_task_ids": tasks,
        "repeats": benchmark["repeats"],
        "expected_pairs": expected_pairs,
        "arms": arms,
        "paired": paired,
        "per_task": per_task,
        "paired_deltas": deltas,
        "metric_observations": metric_observations,
        "score_contract": "finite numeric values from 0 to 1; higher is better",
        "provenance_note": (
            "Observed executor/model conditions and external grades are operator-attested. "
            "Hashes identify content and detect unmatched edits, but a workspace operator can "
            "rewrite records and recompute hashes; they do not authenticate graders or prove "
            "remote model identity."
        ),
    }
