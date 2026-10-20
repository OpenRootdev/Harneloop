"""Compose experimental changes and retrieve evidence-linked observations.

Composition never inherits source validation. Lessons are operator-authored
observations, not universal facts or authenticated external grader verdicts.
"""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

from .candidate import create_candidate, read_candidate, validate_tier, OPEN_CANDIDATE_STATUSES, VALIDATION_TIER_RANK
from .errors import HarneloopError
from .locking import file_lock, harness_lock_path
from .paths import is_protected_candidate_path, is_secret_path
from .runs import read_run
from .versioning import ensure_unit, hash_file
from .yamlio import read_yaml, write_yaml


def _identifier(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or not value[len(prefix):].isdigit():
        raise HarneloopError(f"Invalid {prefix} identifier")
    return value


def _contained(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root.resolve()):
        raise HarneloopError("Evidence or overlay path escapes its harness unit")
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise HarneloopError("Symlinked evidence and overlays are not supported")
    return path


def compose_candidates(unit_root: Path, candidate_ids: list[str], summary: str) -> Path:
    """Union compatible overlays on one base. Conflicts require an agent's edit.

    Sources remain open, the new candidate receives no inherited evidence,
    and normal promotion still requires fresh validation of the composition.
    """
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    if (not isinstance(summary, str) or not summary.strip() or not isinstance(candidate_ids, list)
            or any(not isinstance(item, str) for item in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids) or len(candidate_ids) < 2):
        raise HarneloopError("Composition requires a summary and at least two distinct candidates")
    with file_lock(harness_lock_path(unit_root, "lifecycle")):
        current = read_yaml(unit_root / "unit.yaml").get("current_version")
        records, files, sources = [], {}, []
        for candidate_id in candidate_ids:
            _identifier(candidate_id, "cand-")
            record = read_candidate(unit_root, candidate_id)
            if record.get("base_version") != current or record.get("status") not in OPEN_CANDIDATE_STATUSES - {"needs_rebase"}:
                raise HarneloopError("Composition requires open candidates on the current base")
            overlay = _contained(unit_root, unit_root / "candidates" / candidate_id / "changes")
            inventory = []
            for path in sorted(overlay.rglob("*")):
                _contained(unit_root, path)
                if not path.is_file():
                    continue
                name = path.relative_to(overlay).as_posix()
                if is_protected_candidate_path(name) or is_secret_path(path):
                    raise HarneloopError(f"Composition source contains a protected or secret path: {name}")
                content = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
                if name in files and files[name] != (content, mode):
                    raise HarneloopError(f"Composition conflict at {name}; resolve it explicitly in a new candidate")
                files[name] = (content, mode)
                inventory.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "mode": mode})
            records.append(record)
            sources.append({"candidate_id": candidate_id, "files": inventory})
        planes = {r.get("plane", "mixed") for r in records}
        if len(planes) != 1:
            raise HarneloopError("Keep target-harness, evaluation and infrastructure composition separate")
        if not files:
            raise HarneloopError("Composition has no file changes")
        tier = max((validate_tier(str(r.get("validation_tier", "targeted"))) for r in records), key=VALIDATION_TIER_RANK.__getitem__)
        candidate = create_candidate(unit_root, summary, plane=planes.pop(), validation_tier=tier)
        for name, (content, mode) in files.items():
            target = candidate / "changes" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(mode)
        metadata = read_candidate(unit_root, candidate.name)
        metadata["composition"] = {"sources": sources, "requires_fresh_validation": True}
        write_yaml(candidate / "candidate.yaml", metadata)
        return candidate


def _lesson_evidence(unit_root: Path, run_id: str, artifact_id: str) -> dict:
    _identifier(run_id, "run-")
    _identifier(artifact_id, "artifact-")
    _contained(unit_root, unit_root / "runtime/runs" / run_id / "run.yaml")
    run = read_run(unit_root, run_id)
    if run.get("status") not in {"succeeded", "failed", "stopped"}:
        raise HarneloopError("A lesson requires a finished run")
    artifact = next((a for a in run.get("artifacts", []) if a.get("id") == artifact_id), None)
    if not artifact or not isinstance(artifact.get("stored_path"), str):
        raise HarneloopError("Lesson artifact is not present in the run")
    path = _contained(unit_root, unit_root / artifact["stored_path"])
    if not path.resolve().is_relative_to((unit_root / "runtime/artifacts" / run_id).resolve()):
        raise HarneloopError("Lesson artifact is not in the recorded run's storage")
    if not path.is_file() or hash_file(path) != artifact.get("sha256"):
        raise HarneloopError("Lesson evidence is missing or changed")
    return {"run_id": run_id, "artifact_id": artifact_id, "sha256": artifact["sha256"]}


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def add_lesson(unit_root: Path, cue: str, lesson: str, run_id: str, artifact_id: str) -> dict[str, Any]:
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    if not isinstance(cue, str) or not isinstance(lesson, str) or not cue.strip() or not lesson.strip():
        raise HarneloopError("Lesson cue and observation cannot be empty")
    with file_lock(harness_lock_path(unit_root, "lessons")):
        evidence = _lesson_evidence(unit_root, run_id, artifact_id)
        root = _contained(unit_root, unit_root / ".evolve/lessons")
        root.mkdir(parents=True, exist_ok=True)
        numbers = [int(p.stem[7:]) for p in root.glob("lesson-*.yaml") if p.stem[7:].isdigit()]
        lesson_id = f"lesson-{max(numbers, default=0)+1:04d}"
        row = {"id": lesson_id, "cue": cue.strip(), "lesson": lesson.strip(), "evidence": evidence,
               "scope": "operator-authored observation supported by the referenced artifact"}
        row["sha256"] = _digest(row)
        write_yaml(root / f"{lesson_id}.yaml", row)
        return row


def list_lessons(unit_root: Path, query: str = "") -> list[dict[str, Any]]:
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    if not isinstance(query, str):
        raise HarneloopError("Lesson query must be text")
    results = []
    with file_lock(harness_lock_path(unit_root, "lessons")):
        root = _contained(unit_root, unit_root / ".evolve/lessons")
        for path in sorted(root.glob("lesson-*.yaml")):
            _contained(unit_root, path)
            row = read_yaml(path)
            if (not isinstance(row, dict) or not isinstance(row.get("cue"), str)
                    or not isinstance(row.get("lesson"), str) or not isinstance(row.get("evidence"), dict)
                    or not isinstance(row["evidence"].get("run_id"), str)
                    or not isinstance(row["evidence"].get("artifact_id"), str)):
                raise HarneloopError("Lesson record is malformed; inspect its cue, lesson and evidence references")
            original = {k: v for k, v in row.items() if k != "sha256"}
            if row.get("sha256") != _digest(original):
                raise HarneloopError("Lesson record has changed")
            evidence = _lesson_evidence(unit_root, row["evidence"]["run_id"], row["evidence"]["artifact_id"])
            if evidence != row["evidence"]:
                raise HarneloopError("Lesson evidence changed")
            if not query or query.casefold() in (row["cue"]+" "+row["lesson"]).casefold():
                results.append(row)
    return results
