from __future__ import annotations

from pathlib import Path
from typing import Any

from .candidate import VALIDATION_TIER_RANK, candidate_digest, read_candidate, validate_tier
from .errors import HarneloopError
from .locking import file_lock, harness_lock_path
from .runs import read_run
from .state import now_iso
from .versioning import candidate_root, ensure_unit, hash_file
from .yamlio import read_yaml, write_yaml


def evidence_root(unit_root: Path, candidate_id: str) -> Path:
    root = candidate_root(unit_root, candidate_id) / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    return root


def next_evidence_id(unit_root: Path, candidate_id: str) -> str:
    root = evidence_root(unit_root, candidate_id)
    existing: list[int] = []
    for path in root.glob("evidence-*.yaml"):
        try:
            existing.append(int(path.stem.removeprefix("evidence-")))
        except ValueError:
            continue
    return f"evidence-{(max(existing, default=0) + 1):04d}"


def list_evidence(unit_root: Path, candidate_id: str) -> list[dict[str, Any]]:
    root = evidence_root(unit_root, candidate_id)
    records = [read_yaml(path) for path in sorted(root.glob("evidence-*.yaml"))]
    return [record for record in records if record]


def validate_evidence_references(unit_root: Path, record: dict[str, Any]) -> None:
    run_id = record.get("run_id")
    artifact_id = record.get("artifact_id")
    evidence_path = record.get("path")
    candidate_id = record.get("candidate_id")
    # New evidence records bind all observations to the overlay being assessed.
    # Legacy records without a fingerprint remain readable.
    if candidate_id and record.get("candidate_sha256"):
        if candidate_digest(unit_root, str(candidate_id)) != record["candidate_sha256"]:
            raise HarneloopError("Candidate overlay changed after evidence was attached; collect fresh evidence")

    if artifact_id and not run_id:
        raise HarneloopError(f"Evidence artifact `{artifact_id}` requires a run_id")

    run_record: dict[str, Any] | None = None
    if run_id:
        run_file = unit_root / "runtime" / "runs" / str(run_id) / "run.yaml"
        if not run_file.is_file():
            raise HarneloopError(f"Run does not exist: {run_id}")
        run_record = read_run(unit_root, str(run_id))
        if candidate_id and run_record.get("candidate_id") == candidate_id and run_record.get("candidate_sha256"):
            if candidate_digest(unit_root, str(candidate_id)) != run_record["candidate_sha256"]:
                raise HarneloopError("Candidate overlay changed since the referenced run; rerun validation")

    if artifact_id:
        artifacts = (run_record.get("artifacts") or []) if run_record else []
        artifact = next((item for item in artifacts if item.get("id") == artifact_id), None)
        if artifact is None:
            raise HarneloopError(f"Artifact does not exist in run `{run_id}`: {artifact_id}")
        stored_path = artifact.get("stored_path")
        stored_file = (unit_root / str(stored_path)).resolve() if stored_path else None
        if stored_file is None or not stored_file.is_file():
            raise HarneloopError(f"Artifact stored file does not exist for `{artifact_id}` in run `{run_id}`")
        if not stored_file.is_relative_to(unit_root.resolve()):
            raise HarneloopError("Stored artifact escapes its harness unit")
        if artifact.get("sha256") and hash_file(stored_file) != artifact["sha256"]:
            raise HarneloopError("Stored artifact changed after capture; collect fresh evidence")

    if evidence_path:
        referenced_file = Path(str(evidence_path))
        if not referenced_file.is_absolute():
            referenced_file = unit_root / referenced_file
        if not referenced_file.is_file():
            raise HarneloopError(f"Evidence file does not exist: {referenced_file}")


def add_evidence(
    unit_root: Path,
    candidate_id: str,
    kind: str,
    summary: str,
    outcome: str = "supports",
    run_id: str | None = None,
    artifact_id: str | None = None,
    path: Path | None = None,
    validation_tier: str | None = None,
) -> dict[str, Any]:
    ensure_unit(unit_root)
    if not summary.strip():
        raise HarneloopError("Evidence summary cannot be empty")

    unit_root = unit_root.resolve()
    candidate = read_candidate(unit_root, candidate_id)
    selected_tier = validate_tier(validation_tier or str(candidate.get("validation_tier", "targeted")))
    if selected_tier in {"representative", "full"} and not run_id:
        raise HarneloopError(f"{selected_tier.capitalize()} evidence must reference a recorded run")
    with file_lock(harness_lock_path(unit_root, f"candidate-{candidate_id}-evidence")):
        evidence_id = next_evidence_id(unit_root, candidate_id)
        resolved_path = path.resolve() if path else None
        record: dict[str, Any] = {
            "id": evidence_id,
            "candidate_id": candidate_id,
            "kind": kind,
            "summary": summary,
            "outcome": outcome,
            "validation_tier": selected_tier,
            "candidate_base_version": candidate.get("base_version"),
            "candidate_sha256": candidate_digest(unit_root, candidate_id),
            "run_id": run_id,
            "artifact_id": artifact_id,
            "path": str(resolved_path) if resolved_path else None,
            "created_at": now_iso(),
        }
        validate_evidence_references(unit_root, record)
        write_yaml(evidence_root(unit_root, candidate_id) / f"{evidence_id}.yaml", record)
        return record


def has_promotion_evidence(unit_root: Path, candidate_id: str) -> bool:
    return bool(qualifying_promotion_evidence(unit_root, candidate_id))


def qualifying_promotion_evidence(unit_root: Path, candidate_id: str) -> list[dict[str, Any]]:
    candidate = read_candidate(unit_root, candidate_id)
    records = list_evidence(unit_root, candidate_id)
    supporting = [record for record in records if record.get("outcome") in {"supports", "mixed", "neutral"}]
    for record in supporting:
        validate_evidence_references(unit_root.resolve(), record)
    if candidate.get("schema_version") != "0.2":
        return supporting

    required_tier = str(candidate.get("validation_tier", "targeted"))
    required_rank = VALIDATION_TIER_RANK[required_tier]
    base_version = candidate.get("base_version")
    return [
        record
        for record in supporting
        if record.get("candidate_base_version") == base_version
        and VALIDATION_TIER_RANK.get(str(record.get("validation_tier", "structural")), -1) >= required_rank
    ]
