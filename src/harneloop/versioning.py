from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .candidate import (
    _sync_candidate_state,
    mark_parallel_candidates_needing_rebase,
    read_candidate,
)
from .errors import HarneloopError
from .locking import file_lock, harness_lock_path
from .paths import is_protected_candidate_path, packageable_files, relative_posix
from .state import now_iso
from .yamlio import read_yaml, write_yaml


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_unit(unit_root: Path) -> None:
    if not (unit_root / "unit.yaml").exists():
        raise HarneloopError(f"Not a Harneloop harness unit: {unit_root}")


def candidate_root(unit_root: Path, candidate_id: str) -> Path:
    root = unit_root / "candidates" / candidate_id
    if not root.exists():
        raise HarneloopError(f"Candidate does not exist: {candidate_id}")
    return root


def validate_candidate_overlay(candidate: Path) -> list[str]:
    changes_root = candidate / "changes"
    blocked: list[str] = []
    if not changes_root.exists():
        return blocked
    for file_path in changes_root.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(changes_root).as_posix()
        if is_protected_candidate_path(relative):
            blocked.append(relative)
    return blocked


def apply_candidate_overlay(unit_root: Path, candidate: Path) -> list[str]:
    changes_root = candidate / "changes"
    if not changes_root.exists():
        return []

    blocked = validate_candidate_overlay(candidate)
    if blocked:
        blocked_text = ", ".join(sorted(blocked))
        raise HarneloopError(f"Candidate modifies protected paths: {blocked_text}")

    applied: list[str] = []
    for source in sorted(changes_root.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(changes_root)
        target = unit_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        applied.append(relative.as_posix())
    return applied


def write_manifest(unit_root: Path, snapshot_root: Path, version: str) -> dict[str, object]:
    files = []
    for path in packageable_files(unit_root):
        relative = relative_posix(unit_root, path)
        files.append(
            {
                "path": relative,
                "sha256": hash_file(path),
                "size": path.stat().st_size,
            }
        )
        target = snapshot_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    manifest: dict[str, object] = {
        "version": version,
        "created_at": now_iso(),
        "files": files,
    }
    manifest_path = snapshot_root.parent / "manifest.json"
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=manifest_path.parent,
            prefix=".manifest.json.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, manifest_path)
    finally:
        if temp_path and Path(temp_path).exists():
            Path(temp_path).unlink()
    return manifest


def require_candidate_evidence(unit_root: Path, candidate_id: str) -> None:
    from .evidence import list_evidence, qualifying_promotion_evidence

    if qualifying_promotion_evidence(unit_root, candidate_id):
        return
    candidate = read_candidate(unit_root, candidate_id)
    if candidate.get("schema_version") == "0.2" and list_evidence(unit_root, candidate_id):
        raise HarneloopError(
            f"Candidate promotion requires fresh evidence for base version "
            f"`{candidate.get('base_version') or 'none'}` at validation tier "
            f"`{candidate.get('validation_tier')}` or higher."
        )
    else:
        raise HarneloopError(
            "Candidate promotion requires evidence. Add evidence with "
            "`harneloop candidate evidence add` or pass --allow-missing-evidence for development-only promotion."
        )


def promote_candidate(
    unit_root: Path,
    candidate_id: str,
    version: str,
    summary: str | None = None,
    require_evidence: bool = True,
) -> Path:
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    with file_lock(harness_lock_path(unit_root, "lifecycle")):
        candidate = candidate_root(unit_root, candidate_id)
        candidate_meta_path = candidate / "candidate.yaml"
        candidate_meta = read_yaml(candidate_meta_path)
        if candidate_meta.get("status") == "promoted":
            raise HarneloopError(f"Candidate is already promoted: {candidate_id}")
        unit_meta_path = unit_root / "unit.yaml"
        unit_meta = read_yaml(unit_meta_path)
        previous_version = unit_meta.get("current_version")
        if candidate_meta.get("schema_version") == "0.2":
            status = candidate_meta.get("status")
            if status == "needs_rebase" or candidate_meta.get("base_version") != previous_version:
                raise HarneloopError(
                    f"Candidate `{candidate_id}` must rebase onto current version "
                    f"`{previous_version or 'none'}` before promotion."
                )
            if status not in {"ready", "validating"}:
                raise HarneloopError(
                    f"Candidate `{candidate_id}` is `{status}`; mark it ready before promotion."
                )
        if require_evidence:
            require_candidate_evidence(unit_root, candidate_id)

        version_root = unit_root / "versions" / version
        if version_root.exists():
            raise HarneloopError(f"Version already exists: {version}")

        applied = apply_candidate_overlay(unit_root, candidate)

        unit_meta["current_version"] = version
        unit_meta["updated_at"] = now_iso()
        write_yaml(unit_meta_path, unit_meta)

        candidate_meta["status"] = "promoted"
        candidate_meta["promoted_at"] = now_iso()
        candidate_meta["promoted_version"] = version
        write_yaml(candidate_meta_path, candidate_meta)
        mark_parallel_candidates_needing_rebase(unit_root, candidate_id, version)

        version_root.mkdir(parents=True, exist_ok=False)
        snapshot_root = version_root / "snapshot"
        snapshot_root.mkdir(parents=True, exist_ok=True)
        manifest = write_manifest(unit_root, snapshot_root, version)

        version_summary = summary or candidate_meta.get("summary") or f"Promoted {candidate_id}"
        write_yaml(
            version_root / "version.yaml",
            {
                "version": version,
                "base_version": previous_version,
                "promoted_from": candidate_id,
                "created_at": now_iso(),
                "summary": version_summary,
                "plane": candidate_meta.get("plane", candidate_meta.get("kind", "legacy")),
                "validation_tier": candidate_meta.get("validation_tier", "legacy"),
                "applied_files": applied,
                "manifest": "manifest.json",
            },
        )

        changelog = unit_root / "provenance" / "changelog.md"
        with changelog.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                f"- {now_iso()}: Promoted `{candidate_id}` as `{version}` "
                f"(`{candidate_meta.get('plane', candidate_meta.get('kind', 'legacy'))}` / "
                f"`{candidate_meta.get('validation_tier', 'legacy')}`). {version_summary}\n"
            )
        _sync_candidate_state(
            unit_root,
            focused_candidate=None,
            reason="candidate_promoted",
            next_action="Create another candidate, package the harness unit, or stop if evidence is sufficient.",
            current_version=version,
        )
    return version_root


def rollback_unit(unit_root: Path, version: str) -> Path:
    unit_root = unit_root.resolve()
    ensure_unit(unit_root)
    with file_lock(harness_lock_path(unit_root, "lifecycle")):
        version_root = unit_root / "versions" / version
        snapshot_root = version_root / "snapshot"
        if not snapshot_root.exists():
            raise HarneloopError(f"Version snapshot does not exist: {version}")

        for path in packageable_files(unit_root):
            path.unlink()

        for source in sorted(snapshot_root.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(snapshot_root)
            target = unit_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        changelog = unit_root / "provenance" / "changelog.md"
        with changelog.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"- {now_iso()}: Rolled back working tree to `{version}`.\n")
        mark_parallel_candidates_needing_rebase(unit_root, "", version)
        _sync_candidate_state(
            unit_root,
            focused_candidate=None,
            reason="rollback",
            next_action="Inspect restored harness unit state before creating another candidate.",
            current_version=version,
        )
    return version_root
