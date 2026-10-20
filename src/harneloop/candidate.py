from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from .errors import HarneloopError
from .locking import file_lock, harness_lock_path
from .state import now_iso, update_state
from .yamlio import read_yaml, write_yaml


CANDIDATE_PLANES = {"target_harness", "evaluation", "infrastructure", "mixed"}
VALIDATION_TIERS = ("structural", "targeted", "representative", "full")
VALIDATION_TIER_RANK = {tier: index for index, tier in enumerate(VALIDATION_TIERS)}
OPEN_CANDIDATE_STATUSES = {"draft", "accumulating", "ready", "validating", "needs_rebase"}
TERMINAL_CANDIDATE_STATUSES = {"promoted", "rejected"}
ALLOWED_STATUS_TRANSITIONS = {
    "draft": {"accumulating", "ready", "rejected"},
    "accumulating": {"ready", "rejected"},
    "ready": {"accumulating", "validating", "rejected"},
    "validating": {"accumulating", "ready", "rejected"},
    "needs_rebase": {"rejected"},
}


def validate_plane(plane: str) -> str:
    if plane not in CANDIDATE_PLANES:
        supported = ", ".join(sorted(CANDIDATE_PLANES))
        raise HarneloopError(f"Unknown candidate plane `{plane}`. Expected one of: {supported}")
    return plane


def validate_tier(validation_tier: str) -> str:
    if validation_tier not in VALIDATION_TIER_RANK:
        supported = ", ".join(VALIDATION_TIERS)
        raise HarneloopError(f"Unknown validation tier `{validation_tier}`. Expected one of: {supported}")
    return validation_tier


def candidate_path(unit_root: Path, candidate_id: str) -> Path:
    path = unit_root.resolve() / "candidates" / candidate_id
    if not path.exists():
        raise HarneloopError(f"Candidate does not exist: {candidate_id}")
    return path


def read_candidate(unit_root: Path, candidate_id: str) -> dict[str, object]:
    return read_yaml(candidate_path(unit_root, candidate_id) / "candidate.yaml")


def candidate_digest(unit_root: Path, candidate_id: str) -> str:
    """Fingerprint the candidate overlay files that define its executable changes."""
    unit_root = unit_root.resolve()
    root = candidate_path(unit_root, candidate_id)
    if (unit_root / "candidates").is_symlink() or root.is_symlink() or not root.resolve().is_relative_to(unit_root):
        raise HarneloopError(f"Candidate path escapes the harness unit: {candidate_id}")
    if (root / "candidate.yaml").is_symlink():
        raise HarneloopError(f"Candidate metadata cannot be a symlink: {candidate_id}")
    changes = root / "changes"
    if changes.is_symlink():
        raise HarneloopError(f"Candidate changes path cannot be a symlink: {candidate_id}")
    entries: list[dict[str, object]] = []
    if changes.exists():
        for path in sorted(changes.rglob("*")):
            if path.is_symlink():
                raise HarneloopError(f"Candidate overlay contains a symlink: {path}")
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(changes.resolve()):
                raise HarneloopError(f"Candidate overlay escapes its changes directory: {path}")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries.append(
                {
                    "path": path.relative_to(changes).as_posix(),
                    "sha256": digest.hexdigest(),
                    "size": path.stat().st_size,
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            )
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def list_candidates(unit_root: Path, include_closed: bool = True) -> list[dict[str, object]]:
    candidates_root = unit_root.resolve() / "candidates"
    if not candidates_root.exists():
        return []
    records = [read_yaml(path / "candidate.yaml") for path in sorted(candidates_root.glob("cand-*")) if path.is_dir()]
    records = [record for record in records if record]
    if not include_closed:
        records = [record for record in records if record.get("status") in OPEN_CANDIDATE_STATUSES]
    return records


def active_candidate_ids(unit_root: Path) -> list[str]:
    return [str(record["id"]) for record in list_candidates(unit_root, include_closed=False)]


def _sync_candidate_state(unit_root: Path, focused_candidate: str | None, reason: str, next_action: str) -> None:
    active = active_candidate_ids(unit_root)
    focused = focused_candidate if focused_candidate in active else (active[-1] if active else None)
    update_state(
        unit_root,
        state="active",
        active_candidates=active,
        active_candidate=focused,
        reason=reason,
        next_action=next_action,
    )


def next_candidate_id(unit_root: Path) -> str:
    candidates_root = unit_root / "candidates"
    candidates_root.mkdir(parents=True, exist_ok=True)
    existing = []
    for path in candidates_root.iterdir():
        if path.is_dir() and path.name.startswith("cand-"):
            try:
                existing.append(int(path.name.removeprefix("cand-")))
            except ValueError:
                continue
    return f"cand-{(max(existing, default=0) + 1):04d}"


def create_candidate(
    unit_root: Path,
    summary: str,
    kind: str = "mixed",
    *,
    plane: str | None = None,
    validation_tier: str = "targeted",
) -> Path:
    unit_root = unit_root.resolve()
    if not (unit_root / "unit.yaml").exists():
        raise HarneloopError(f"Not a Harneloop harness unit: {unit_root}")
    selected_plane = validate_plane(plane or (kind if kind in CANDIDATE_PLANES else "mixed"))
    selected_tier = validate_tier(validation_tier)

    with file_lock(harness_lock_path(unit_root, "candidates")):
        candidate_id = next_candidate_id(unit_root)
        candidate_root = unit_root / "candidates" / candidate_id
        candidate_root.mkdir(parents=True, exist_ok=False)
        for directory in ["changes", "validation", "evidence"]:
            (candidate_root / directory).mkdir(parents=True, exist_ok=True)

        unit_meta = read_yaml(unit_root / "unit.yaml")
        write_yaml(
            candidate_root / "candidate.yaml",
            {
                "schema_version": "0.2",
                "id": candidate_id,
                "base_version": unit_meta.get("current_version"),
                "kind": kind,
                "plane": selected_plane,
                "validation_tier": selected_tier,
                "status": "accumulating",
                "summary": summary,
                "created_at": now_iso(),
                "changes": {"mode": "overlay", "path": "changes/"},
                "evidence_required": ["rationale", "validation_notes"],
            },
        )
        (candidate_root / "rationale.md").write_text(
            f"# Rationale\n\n{summary}\n", encoding="utf-8", newline="\n"
        )
        (candidate_root / "notes.md").write_text("# Notes\n", encoding="utf-8", newline="\n")

    _sync_candidate_state(
        unit_root,
        focused_candidate=candidate_id,
        reason="candidate_created",
        next_action=f"Accumulate related changes in `{candidate_id}` and mark it ready when its validation is sufficient.",
    )
    return candidate_root


def set_candidate_status(unit_root: Path, candidate_id: str, status: str) -> dict[str, object]:
    if status not in set().union(*ALLOWED_STATUS_TRANSITIONS.values()):
        supported = ", ".join(sorted(set().union(*ALLOWED_STATUS_TRANSITIONS.values())))
        raise HarneloopError(f"Unsupported candidate status `{status}`. Expected one of: {supported}")
    unit_root = unit_root.resolve()
    with file_lock(harness_lock_path(unit_root, f"candidate-{candidate_id}")):
        path = candidate_path(unit_root, candidate_id) / "candidate.yaml"
        record = read_yaml(path)
        current = str(record.get("status", "draft"))
        allowed = ALLOWED_STATUS_TRANSITIONS.get(current, set())
        if status not in allowed:
            raise HarneloopError(f"Candidate `{candidate_id}` cannot transition from `{current}` to `{status}`")
        record["status"] = status
        record["status_updated_at"] = now_iso()
        write_yaml(path, record)

    _sync_candidate_state(
        unit_root,
        focused_candidate=candidate_id if status != "rejected" else None,
        reason=f"candidate_{status}",
        next_action=(
            f"Validate and promote `{candidate_id}` when its required evidence is attached."
            if status in {"ready", "validating"}
            else "Continue the remaining candidate work or create another coherent candidate."
        ),
    )
    return record


def rebase_candidate(unit_root: Path, candidate_id: str) -> dict[str, object]:
    unit_root = unit_root.resolve()
    with file_lock(harness_lock_path(unit_root, f"candidate-{candidate_id}")):
        path = candidate_path(unit_root, candidate_id) / "candidate.yaml"
        record = read_yaml(path)
        status = str(record.get("status", "draft"))
        if status in TERMINAL_CANDIDATE_STATUSES:
            raise HarneloopError(f"Closed candidate cannot be rebased: {candidate_id}")
        current_version = read_yaml(unit_root / "unit.yaml").get("current_version")
        previous_base = record.get("base_version")
        if previous_base == current_version and status != "needs_rebase":
            raise HarneloopError(f"Candidate `{candidate_id}` already uses the current base version")
        history = list(record.get("rebase_history") or [])
        history.append({"from": previous_base, "to": current_version, "rebased_at": now_iso()})
        record["base_version"] = current_version
        record["status"] = "accumulating"
        record["rebase_history"] = history
        record["status_updated_at"] = now_iso()
        write_yaml(path, record)

    _sync_candidate_state(
        unit_root,
        focused_candidate=candidate_id,
        reason="candidate_rebased",
        next_action=f"Revalidate `{candidate_id}` against base version `{current_version or 'none'}` before promotion.",
    )
    return record


def mark_parallel_candidates_needing_rebase(unit_root: Path, promoted_id: str, new_version: str) -> list[str]:
    stale: list[str] = []
    for record in list_candidates(unit_root, include_closed=False):
        candidate_id = str(record.get("id"))
        if candidate_id == promoted_id or record.get("base_version") == new_version:
            continue
        with file_lock(harness_lock_path(unit_root, f"candidate-{candidate_id}")):
            path = candidate_path(unit_root, candidate_id) / "candidate.yaml"
            current = read_yaml(path)
            if current.get("status") not in OPEN_CANDIDATE_STATUSES:
                continue
            current["status"] = "needs_rebase"
            current["status_updated_at"] = now_iso()
            write_yaml(path, current)
        stale.append(candidate_id)
    return stale
