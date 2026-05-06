from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml

from aic_collector.job_queue import QueueState, legacy_dir, queue_dir
from aic_collector.job_queue import write_plans
from aic_collector.sampler import sample_scenes


class PresetError(ValueError):
    """Raised when a preset file cannot be loaded or validated."""


class SlotExhausted(PresetError):
    """Raised when no more preset slots are available."""


@dataclass(frozen=True)
class TrialSpec:
    """Per-trial task dispatch + fixed target.

    A trial id (e.g. "trial_1") binds a task_type and a (rail, port) pair so
    every member assigned to that trial collects the same scenario family.
    """

    trial_id: str
    task_type: Literal["sfp", "sc"]
    rail: int
    port: str


@dataclass(frozen=True)
class MemberAssignment:
    """Per-member trial allotment."""

    trial_id: str
    count: int


@dataclass(frozen=True)
class TeamPreset:
    base_seed: int
    shard_stride: int
    index_width: int
    strategy: Literal["uniform", "lhs"]
    ranges: Mapping[str, Any]
    scene: Mapping[str, Any]
    tasks: Mapping[str, int]
    members: tuple[Mapping[str, Any], ...]
    preset_hash: str
    trials: Mapping[str, TrialSpec] = MappingProxyType({})
    member_assignments: Mapping[str, tuple[MemberAssignment, ...]] = MappingProxyType({})


@dataclass(frozen=True)
class SubmitResult:
    start_index: int
    written_count: int
    entry_id: int


_CONFIG_INDEX_RE_TEMPLATE = r"config_{task_type}_(\d+)\.yaml"


def _canonical_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _ledger_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PresetError(f"Malformed ledger YAML: {path}") from exc

    if raw is None:
        return []
    if not isinstance(raw, dict) or "entries" not in raw or not isinstance(raw["entries"], list):
        raise PresetError(f"Invalid ledger YAML shape: {path}")

    entries: list[dict[str, Any]] = []
    for entry in raw["entries"]:
        if not isinstance(entry, dict):
            raise PresetError(f"Invalid ledger entry in: {path}")
        entries.append(dict(entry))
    return entries


def _atomic_rewrite(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        yaml.safe_dump(payload, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes"}


def _enforce_repro_gates(
    entries: list[dict[str, Any]],
    *,
    task_type: str,
    preset_hash: str,
    git_sha: str,
) -> None:
    """Refuse non-reproducible submits.

    `dirty:<sha>` => raises unless AIC_ALLOW_DIRTY env flag is truthy.
    `uncommitted` (no git checkout) is permitted — the 2026-04-20 spec
    explicitly supports it for wheel installs.

    preset_hash drift vs. the most recent same-task_type entry => raises
    unless AIC_ALLOW_PRESET_DRIFT env flag is truthy. First-ever submit
    has no prior entry to compare against, so it passes.
    """
    if git_sha.startswith("dirty:") and not _env_flag("AIC_ALLOW_DIRTY"):
        raise PresetError(
            "Refusing to submit on a dirty tree. Commit or stash, "
            "or set AIC_ALLOW_DIRTY=1 to override."
        )
    same_task = [e for e in entries if e.get("task_type") == task_type]
    if same_task:
        prior_hash = same_task[-1].get("preset_hash")
        if prior_hash != preset_hash and not _env_flag("AIC_ALLOW_PRESET_DRIFT"):
            raise PresetError(
                f"preset_hash drift vs latest {task_type} entry "
                f"({prior_hash!r} -> {preset_hash!r}). "
                "Set AIC_ALLOW_PRESET_DRIFT=1 to override."
            )


def _git_sha() -> str:
    repo_root = Path(__file__).resolve().parent.parent.parent
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "uncommitted"

    return f"dirty:{sha}" if status else sha


def _lock_path(ledger_path: Path) -> Path:
    return ledger_path.with_suffix(f"{ledger_path.suffix}.lock")


def _require_valid_entry_id(entries: list[dict[str, Any]], entry_id: int) -> None:
    if entry_id < 0 or entry_id >= len(entries):
        raise PresetError(f"Invalid ledger entry id: {entry_id}")


@contextmanager
def _ledger_lock(ledger_path: Path) -> Any:
    lock_path = _lock_path(ledger_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _write_ledger_entries(ledger_path: Path, entries: list[dict[str, Any]]) -> None:
    _atomic_rewrite(ledger_path, {"entries": entries})


def _require_path(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise PresetError(f"Missing required field: {path}")
        current = current[part]
    return current


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PresetError(f"Invalid integer field: {path}")
    return value


def _validate_non_negative_int(value: Any, path: str) -> int:
    validated = _validate_int(value, path)
    if validated < 0:
        raise PresetError(f"Invalid integer field: {path}")
    return validated


def _validate_positive_int(value: Any, path: str) -> int:
    validated = _validate_int(value, path)
    if validated <= 0:
        raise PresetError(f"Invalid integer field: {path}")
    return validated


def _validate_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresetError(f"Invalid mapping field: {path}")
    return value


def _validate_strategy(value: Any) -> Literal["uniform", "lhs"]:
    if value not in {"uniform", "lhs"}:
        raise PresetError("Invalid strategy field: sampling.strategy")
    return value


def _validate_tasks(value: Any) -> dict[str, int]:
    tasks = _validate_mapping(value, "tasks")
    validated: dict[str, int] = {}
    for key, task_count in tasks.items():
        validated[str(key)] = _validate_int(task_count, f"tasks.{key}")
    return validated


def _validate_members(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise PresetError("Invalid members field: members")

    members: list[Mapping[str, Any]] = []
    member_ids: set[str] = set()
    for index, member in enumerate(value):
        if not isinstance(member, dict):
            raise PresetError(f"Invalid member field: members[{index}]")
        if "id" not in member:
            raise PresetError(f"Missing required field: members[{index}].id")
        if "name" not in member:
            raise PresetError(f"Missing required field: members[{index}].name")
        if member["id"] is None:
            raise PresetError(f"Invalid member field: members[{index}].id")
        if member["name"] is None:
            raise PresetError(f"Invalid member field: members[{index}].name")

        normalized: dict[str, Any] = {}
        for k, v in member.items():
            key = str(k)
            if key == "assignment":
                normalized[key] = v if isinstance(v, dict) else {}
            elif key == "assignments":
                normalized[key] = v if isinstance(v, list) else []
            else:
                normalized[key] = str(v) if v is not None else None
        member_id = normalized["id"]
        if member_id in member_ids:
            raise PresetError(f"Invalid members field: duplicate member id: {member_id}")
        member_ids.add(member_id)
        members.append(MappingProxyType(normalized))
    return tuple(members)


_TRIAL_PORT_RE = re.compile(r"^(sfp_port|sc_port)_\d+$")


def _validate_trials(value: Any) -> dict[str, TrialSpec]:
    """Optional `trials` block. Empty/missing returns {}."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PresetError("Invalid trials field: trials")

    out: dict[str, TrialSpec] = {}
    for raw_id, spec in value.items():
        trial_id = str(raw_id)
        if not isinstance(spec, dict):
            raise PresetError(f"Invalid trials field: trials.{trial_id}")
        task_type = spec.get("task_type")
        if task_type not in ("sfp", "sc"):
            raise PresetError(f"trials.{trial_id}.task_type must be 'sfp' or 'sc'")
        ft = spec.get("fixed_target")
        if not isinstance(ft, dict):
            raise PresetError(f"trials.{trial_id}.fixed_target must be a mapping")
        rail = ft.get("rail")
        port = ft.get("port")
        if isinstance(rail, bool) or not isinstance(rail, int) or rail < 0:
            raise PresetError(f"trials.{trial_id}.fixed_target.rail must be a non-negative int")
        if not isinstance(port, str) or not _TRIAL_PORT_RE.match(port):
            raise PresetError(
                f"trials.{trial_id}.fixed_target.port must match sfp_port_N or sc_port_N"
            )
        out[trial_id] = TrialSpec(
            trial_id=trial_id, task_type=task_type, rail=int(rail), port=port,
        )
    return out


def _validate_one_assignment(
    raw: Any, *, trials: Mapping[str, TrialSpec], path: str,
) -> MemberAssignment:
    if not isinstance(raw, dict):
        raise PresetError(f"Invalid assignment field: {path}")
    trial_id = raw.get("trial")
    count = raw.get("count")
    if not isinstance(trial_id, str):
        raise PresetError(f"{path}.trial must be a string")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise PresetError(f"{path}.count must be a positive int")
    if trial_id not in trials:
        raise PresetError(f"{path}.trial '{trial_id}' is not declared in trials")
    return MemberAssignment(trial_id=trial_id, count=int(count))


def _extract_assignments(
    members: tuple[Mapping[str, Any], ...],
    trials: Mapping[str, TrialSpec],
) -> dict[str, tuple[MemberAssignment, ...]]:
    """Read each member's optional assignment block(s).

    Accepts either `assignment:` (single mapping) or `assignments:` (list of
    mappings). Members without any assignment are omitted from the returned
    mapping. The two forms are mutually exclusive per member.
    """
    out: dict[str, tuple[MemberAssignment, ...]] = {}
    for index, member in enumerate(members):
        single = member.get("assignment")
        multi = member.get("assignments")
        if single and multi:
            raise PresetError(
                f"members[{index}]: use either `assignment` or `assignments`, not both"
            )
        member_id = str(member["id"])
        if multi:
            if not isinstance(multi, list) or not multi:
                raise PresetError(f"members[{index}].assignments must be a non-empty list")
            assignments = tuple(
                _validate_one_assignment(
                    item, trials=trials, path=f"members[{index}].assignments[{i}]"
                )
                for i, item in enumerate(multi)
            )
            seen_trials: set[str] = set()
            for a in assignments:
                if a.trial_id in seen_trials:
                    raise PresetError(
                        f"members[{index}].assignments has duplicate trial: {a.trial_id}"
                    )
                seen_trials.add(a.trial_id)
            out[member_id] = assignments
        elif single:
            out[member_id] = (
                _validate_one_assignment(
                    single, trials=trials, path=f"members[{index}].assignment"
                ),
            )
    return out


def _validate_scene(value: Any) -> dict[str, Any]:
    scene = _validate_mapping(value, "scene")
    fixed_target = scene.get("fixed_target")
    target_cycling = scene.get("target_cycling", True)
    has_concrete_target = isinstance(fixed_target, dict) and any(
        v is not None for v in fixed_target.values()
    )
    if not has_concrete_target and target_cycling is False:
        raise PresetError(
            "scene.target_cycling must be true when scene.fixed_target "
            "has no concrete (rail, port) entries"
        )
    return scene


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, list):
        return [_thaw(item) for item in value]
    return value


def _training_cfg_from_preset(
    preset: TeamPreset,
    trial_spec: TrialSpec | None = None,
) -> dict[str, Any]:
    scene_cfg = _thaw(preset.scene)
    collection_cfg: dict[str, Any] = {}
    fixed_target = scene_cfg.pop("fixed_target", None)
    if fixed_target is not None:
        collection_cfg["fixed_target"] = _thaw(fixed_target)

    if trial_spec is not None:
        collection_cfg["fixed_target"] = {
            trial_spec.task_type: {"rail": trial_spec.rail, "port": trial_spec.port}
        }

    training_cfg: dict[str, Any] = {
        "training": {
            "scene": scene_cfg,
            "ranges": _thaw(preset.ranges),
            "param_strategy": preset.strategy,
        }
    }
    if collection_cfg:
        training_cfg["training"]["collection"] = collection_cfg
    return training_cfg


def _member_index(preset: TeamPreset, member_id: str) -> int:
    for index, member in enumerate(preset.members):
        if member.get("id") == member_id:
            return index
    raise KeyError(member_id)


def _append_claim_locked(
    entries: list[dict[str, Any]],
    *,
    member_id: str,
    task_type: str,
    base_seed: int,
    start_index: int,
    count: int,
    strategy: str,
    queue_root: Path,
    preset_hash: str,
) -> int:
    entry_id = len(entries)
    entries.append(
        {
            "member_id": member_id,
            "task_type": task_type,
            "base_seed": base_seed,
            "start_index": start_index,
            "count": count,
            "strategy": strategy,
            "queue_root": str(queue_root),
            "preset_hash": preset_hash,
            "git_sha": _git_sha(),
            "created_at": _iso_utc_now(),
        }
    )
    return entry_id


def _rollback_claim_locked(entries: list[dict[str, Any]], entry_id: int) -> None:
    _require_valid_entry_id(entries, entry_id)
    if entry_id == len(entries) - 1:
        entries.pop()


def _adjust_claim_count_locked(
    entries: list[dict[str, Any]], entry_id: int, actual_count: int
) -> None:
    _require_valid_entry_id(entries, entry_id)
    entries[entry_id]["count"] = actual_count


def append_claim(
    ledger_path: Path,
    *,
    member_id: str,
    task_type: str,
    base_seed: int,
    start_index: int,
    count: int,
    strategy: str,
    queue_root: Path,
    preset_hash: str,
) -> int:
    with _ledger_lock(ledger_path):
        entries = _ledger_entries(ledger_path)
        entry_id = _append_claim_locked(
            entries,
            member_id=member_id,
            task_type=task_type,
            base_seed=base_seed,
            start_index=start_index,
            count=count,
            strategy=strategy,
            queue_root=queue_root,
            preset_hash=preset_hash,
        )
        _write_ledger_entries(ledger_path, entries)
        return entry_id


def rollback_claim(ledger_path: Path, entry_id: int) -> None:
    with _ledger_lock(ledger_path):
        entries = _ledger_entries(ledger_path)
        _rollback_claim_locked(entries, entry_id)
        _write_ledger_entries(ledger_path, entries)


def adjust_claim_count(ledger_path: Path, entry_id: int, actual_count: int) -> None:
    with _ledger_lock(ledger_path):
        entries = _ledger_entries(ledger_path)
        _adjust_claim_count_locked(entries, entry_id, actual_count)
        _write_ledger_entries(ledger_path, entries)


_RUN_DIR_RE = re.compile(r"^run_\d+_\d+_(sfp|sc)_(\d+)$")


def _trial_total_from_scoring_run(path: Path) -> float | None:
    """Read scoring_run.yaml and return the maximum trial_<N>.total value.

    Queue mode produces 1 trial per run, so max == only-trial. Returns None
    when the file is absent, malformed, or has no scorable trial entry.
    """
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    totals: list[float] = []
    for key, value in data.items():
        if not isinstance(key, str) or not key.startswith("trial_"):
            continue
        if not isinstance(value, dict):
            continue
        # Reuse the same tier-sum convention as postprocess_run.split_scoring.
        tier_scores: list[float] = []
        for tier_key in ("tier_1", "tier_2", "tier_3"):
            tier = value.get(tier_key)
            if isinstance(tier, dict) and isinstance(tier.get("score"), (int, float)):
                tier_scores.append(float(tier["score"]))
        if tier_scores:
            totals.append(sum(tier_scores))
        elif isinstance(value.get("total"), (int, float)):
            totals.append(float(value["total"]))
    if not totals:
        return None
    return max(totals)


def _scan_run_scores(output_root: Path) -> dict[str, dict[int, float]]:
    """Walk output_root for run_*_<task>_<index>/scoring_run.yaml.

    Returns: {task_type: {sample_index: trial_total_score}}. Indices that
    appear in multiple runs (re-collected) keep the *latest* score by run
    directory name (timestamp-prefixed sort).
    """
    by_task: dict[str, dict[int, float]] = {"sfp": {}, "sc": {}}
    if not output_root.exists():
        return by_task
    # Sort so later timestamps overwrite earlier scores.
    for child in sorted(output_root.iterdir()):
        if not child.is_dir():
            continue
        m = _RUN_DIR_RE.match(child.name)
        if not m:
            continue
        task_type, idx_str = m.group(1), m.group(2)
        score = _trial_total_from_scoring_run(child / "scoring_run.yaml")
        if score is None:
            continue
        by_task.setdefault(task_type, {})[int(idx_str)] = score
    return by_task


def reconcile_with_score_threshold(
    ledger_path: Path,
    output_root: Path,
    *,
    threshold: float = 95.0,
) -> list[dict[str, Any]]:
    """Annotate every ledger entry with score-gate metadata.

    For each entry whose sample_index window contains run outputs in
    `output_root`, computes trial total scores from scoring_run.yaml and
    records:
      - low_score_indices:    sorted list of indices with score < threshold
      - high_score_indices:   sorted list of indices with score >= threshold
      - missing_indices:      claimed indices that have no scored run yet
      - score_validated_count: len(high_score_indices)
      - score_threshold:      the threshold used
      - score_reconciled_at:  ISO-UTC timestamp

    Idempotent: re-running with the same on-disk state produces the same
    payload modulo `score_reconciled_at`. Adds metadata only — does not
    touch failed_indices or validated_count from queue-state reconciliation.
    """
    if threshold < 0:
        raise ValueError(f"threshold must be non-negative (got {threshold})")
    with _ledger_lock(ledger_path):
        entries = _ledger_entries(ledger_path)
        scores_by_task = _scan_run_scores(output_root)

        now = _iso_utc_now()
        for entry in entries:
            task_type = entry.get("task_type")
            start_index = entry.get("start_index")
            count = entry.get("count")
            if (
                not isinstance(task_type, str)
                or isinstance(start_index, bool)
                or isinstance(count, bool)
                or not isinstance(start_index, int)
                or not isinstance(count, int)
                or count <= 0
            ):
                continue
            window_end = start_index + count
            task_scores = scores_by_task.get(task_type, {})
            low: list[int] = []
            high: list[int] = []
            missing: list[int] = []
            for idx in range(start_index, window_end):
                if idx not in task_scores:
                    missing.append(idx)
                elif task_scores[idx] < threshold:
                    low.append(idx)
                else:
                    high.append(idx)
            entry["low_score_indices"] = low
            entry["high_score_indices"] = high
            entry["missing_indices"] = missing
            entry["score_validated_count"] = len(high)
            entry["score_threshold"] = float(threshold)
            entry["score_reconciled_at"] = now

        _write_ledger_entries(ledger_path, entries)
        return entries


def requeue_low_score_for_member(
    preset: TeamPreset,
    *,
    member_id: str,
    queue_root: Path,
    ledger_path: Path,
    template_path: Path,
) -> tuple[SubmitResult, ...]:
    """Submit replacement batches for each of a member's trial assignments.

    For every trial id this member is assigned to, sums low_score_indices
    across the matching ledger entries and issues one submit_team_claim
    sized to that count. Returns one SubmitResult per trial that needed
    replacements (in trial declaration order). Returns an empty tuple
    when nothing needs to be re-queued.
    """
    assignments = preset.member_assignments.get(member_id)
    if not assignments:
        raise PresetError(f"Member has no assignment: {member_id}")

    with _ledger_lock(ledger_path):
        entries = _ledger_entries(ledger_path)

    results: list[SubmitResult] = []
    for a in assignments:
        trial_spec = preset.trials.get(a.trial_id)
        if trial_spec is None:
            raise PresetError(f"Assignment references unknown trial: {a.trial_id}")
        lows: list[int] = []
        for entry in entries:
            if entry.get("member_id") != member_id:
                continue
            if entry.get("trial_id") != a.trial_id:
                continue
            lows.extend(int(i) for i in entry.get("low_score_indices") or [])
        if not lows:
            continue
        result = submit_team_claim(
            preset,
            member_id=member_id,
            task_type=trial_spec.task_type,
            queue_root=queue_root,
            ledger_path=ledger_path,
            template_path=template_path,
            trial_spec=trial_spec,
            requested_count=len(lows),
        )
        results.append(result)
    return tuple(results)


def reconcile_ledger_with_queue(
    ledger_path: Path,
    queue_root: Path,
) -> list[dict[str, Any]]:
    """Annotate every ledger entry with simulator-stage failure data.

    Scans `<queue_root>/<task_type>/failed/config_<task>_NNNNNN.yaml` and
    sets, for each entry whose `[start_index, start_index + count)` window
    contains failed indices:
      - failed_indices:  sorted list of in-window failed sample indices
      - validated_count: count - len(failed_indices)
      - reconciled_at:   ISO-UTC timestamp of this reconciliation pass

    Idempotent: re-running with the same `failed/` contents produces the
    same payload modulo `reconciled_at`. Missing or absent `failed/`
    directories are treated as zero failures.

    Returns the updated entries list (same objects that were written to
    disk; safe for the caller to iterate after the lock is released).
    """
    with _ledger_lock(ledger_path):
        entries = _ledger_entries(ledger_path)

        # Cache key is task_type only: queue_root is the single arg passed in,
        # so all entries share the same failed/ directory regardless of the
        # entry["queue_root"] value recorded at submit time.
        failed_by_task: dict[str, list[int]] = {}
        for entry in entries:
            task_type = entry.get("task_type")
            if not isinstance(task_type, str) or task_type in failed_by_task:
                continue
            failed_dir = queue_dir(queue_root, task_type, QueueState.FAILED)
            indices: list[int] = []
            if failed_dir.exists():
                pattern = re.compile(
                    _CONFIG_INDEX_RE_TEMPLATE.format(task_type=re.escape(task_type))
                )
                for path in failed_dir.iterdir():
                    match = pattern.fullmatch(path.name)
                    if match is not None:
                        indices.append(int(match.group(1)))
            failed_by_task[task_type] = sorted(indices)

        now = _iso_utc_now()
        for entry in entries:
            task_type = entry.get("task_type")
            start_index = entry.get("start_index")
            count = entry.get("count")
            if (
                not isinstance(task_type, str)
                or isinstance(start_index, bool)
                or isinstance(count, bool)
                or not isinstance(start_index, int)
                or not isinstance(count, int)
            ):
                continue
            window_end = start_index + count
            in_window = [
                idx
                for idx in failed_by_task.get(task_type, [])
                if start_index <= idx < window_end
            ]
            entry["failed_indices"] = in_window
            entry["validated_count"] = max(0, count - len(in_window))
            entry["reconciled_at"] = now

        _write_ledger_entries(ledger_path, entries)
        return entries


def slot_range(preset: TeamPreset, member_id: str) -> tuple[int, int]:
    member_index = _member_index(preset, member_id)
    slot_start = member_index * preset.shard_stride
    return slot_start, slot_start + preset.shard_stride


def _next_start_index_from_highest_claimed(
    highest_index: int | None,
    *,
    member_id: str,
    slot_start: int,
    slot_end_exclusive: int,
) -> int:
    if highest_index is None:
        return slot_start
    next_index = highest_index + 1
    if next_index >= slot_end_exclusive:
        raise SlotExhausted(f"No remaining slot capacity for member: {member_id}")
    return next_index


def _highest_claimed_index_in_slot(
    entries: list[dict[str, Any]],
    *,
    member_id: str,
    task_type: str,
    slot_start: int,
    slot_end_exclusive: int,
) -> int | None:
    highest_index: int | None = None

    for entry in entries:
        if entry.get("member_id") != member_id or entry.get("task_type") != task_type:
            continue

        start_index = entry.get("start_index")
        count = entry.get("count")
        if (
            isinstance(start_index, bool)
            or isinstance(count, bool)
            or not isinstance(start_index, int)
            or not isinstance(count, int)
            or count <= 0
        ):
            continue

        end_index = start_index + count - 1
        if end_index < slot_start or start_index >= slot_end_exclusive:
            continue

        highest_index = (
            min(end_index, slot_end_exclusive - 1)
            if highest_index is None
            else max(highest_index, min(end_index, slot_end_exclusive - 1))
        )

    return highest_index


def _highest_queued_index_in_slot(
    preset: TeamPreset,
    member_id: str,
    queue_root: Path,
    task_type: str,
) -> int | None:
    slot_start, slot_end_exclusive = slot_range(preset, member_id)
    pattern = re.compile(_CONFIG_INDEX_RE_TEMPLATE.format(task_type=re.escape(task_type)))
    highest_index: int | None = None

    dirs = [queue_dir(queue_root, task_type, state) for state in QueueState]
    dirs.append(legacy_dir(queue_root, task_type))

    for directory in dirs:
        if not directory.exists():
            continue
        for path in directory.iterdir():
            match = pattern.fullmatch(path.name)
            if match is None:
                continue
            config_index = int(match.group(1))
            if slot_start <= config_index < slot_end_exclusive:
                highest_index = (
                    config_index
                    if highest_index is None
                    else max(highest_index, config_index)
                )

    return highest_index


def next_start_index_in_slot(
    preset: TeamPreset,
    member_id: str,
    queue_root: Path,
    task_type: str,
    *,
    ledger_path: Path | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> int:
    slot_start, slot_end_exclusive = slot_range(preset, member_id)
    highest_index = _highest_queued_index_in_slot(preset, member_id, queue_root, task_type)

    if entries is None and ledger_path is not None:
        entries = _ledger_entries(ledger_path)
    if entries is not None:
        highest_claimed_index = _highest_claimed_index_in_slot(
            entries,
            member_id=member_id,
            task_type=task_type,
            slot_start=slot_start,
            slot_end_exclusive=slot_end_exclusive,
        )
        if highest_claimed_index is not None:
            highest_index = (
                highest_claimed_index
                if highest_index is None
                else max(highest_index, highest_claimed_index)
            )

    return _next_start_index_from_highest_claimed(
        highest_index,
        member_id=member_id,
        slot_start=slot_start,
        slot_end_exclusive=slot_end_exclusive,
    )


def _count_files_in_range(
    queue_root: Path,
    task_type: str,
    *,
    start_index: int,
    count: int,
) -> int:
    end_index = start_index + count
    pattern = re.compile(_CONFIG_INDEX_RE_TEMPLATE.format(task_type=re.escape(task_type)))
    matched: set[int] = set()

    dirs = [queue_dir(queue_root, task_type, state) for state in QueueState]
    dirs.append(legacy_dir(queue_root, task_type))
    for directory in dirs:
        if not directory.exists():
            continue
        for path in directory.iterdir():
            match = pattern.fullmatch(path.name)
            if match is None:
                continue
            config_index = int(match.group(1))
            if start_index <= config_index < end_index:
                matched.add(config_index)
    return len(matched)


def submit_team_claim(
    preset: TeamPreset,
    *,
    member_id: str,
    task_type: str,
    queue_root: Path,
    ledger_path: Path,
    template_path: Path,
    trial_spec: TrialSpec | None = None,
    requested_count: int | None = None,
) -> SubmitResult:
    if requested_count is None:
        requested_count = preset.tasks[task_type]
    with _ledger_lock(ledger_path):
        entries = _ledger_entries(ledger_path)
        start_index = next_start_index_in_slot(
            preset,
            member_id,
            queue_root,
            task_type,
            entries=entries,
        )
        _, slot_end_exclusive = slot_range(preset, member_id)
        if start_index + requested_count > slot_end_exclusive:
            raise SlotExhausted(f"No remaining slot capacity for member: {member_id}")

        _enforce_repro_gates(
            entries,
            task_type=task_type,
            preset_hash=preset.preset_hash,
            git_sha=_git_sha(),
        )

        entry_id = _append_claim_locked(
            entries,
            member_id=member_id,
            task_type=task_type,
            base_seed=preset.base_seed,
            start_index=start_index,
            count=requested_count,
            strategy=preset.strategy,
            queue_root=queue_root,
            preset_hash=preset.preset_hash,
        )
        if trial_spec is not None:
            entries[entry_id]["trial_id"] = trial_spec.trial_id
            entries[entry_id]["fixed_target"] = {
                "rail": trial_spec.rail,
                "port": trial_spec.port,
            }
        _write_ledger_entries(ledger_path, entries)

        try:
            plans = sample_scenes(
                _training_cfg_from_preset(preset, trial_spec=trial_spec),
                task_type,
                requested_count,
                preset.base_seed,
                start_index=start_index,
            )
        except Exception:
            _rollback_claim_locked(entries, entry_id)
            _write_ledger_entries(ledger_path, entries)
            raise

        try:
            written_paths = write_plans(
                plans,
                queue_root,
                template_path,
                index_width=preset.index_width,
            )
        except Exception:
            actual_count = _count_files_in_range(
                queue_root,
                task_type,
                start_index=start_index,
                count=requested_count,
            )
            _adjust_claim_count_locked(entries, entry_id, actual_count)
            _write_ledger_entries(ledger_path, entries)
            raise

        return SubmitResult(
            start_index=start_index,
            written_count=len(written_paths),
            entry_id=entry_id,
        )


def load_preset(path: Path) -> TeamPreset | None:
    if not path.exists():
        return None

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PresetError(f"Malformed preset YAML: {path}") from exc

    if not isinstance(raw, dict):
        raise PresetError("Preset root must be a mapping")

    base_seed = _validate_non_negative_int(_require_path(raw, "team.base_seed"), "team.base_seed")
    shard_stride = _validate_positive_int(_require_path(raw, "team.shard_stride"), "team.shard_stride")
    index_width = _validate_positive_int(_require_path(raw, "team.index_width"), "team.index_width")
    strategy = _validate_strategy(_require_path(raw, "sampling.strategy"))
    ranges = _freeze(_validate_mapping(_require_path(raw, "sampling.ranges"), "sampling.ranges"))
    scene = _freeze(_validate_scene(_require_path(raw, "scene")))
    tasks = _freeze(_validate_tasks(_require_path(raw, "tasks")))
    members = _validate_members(_require_path(raw, "members"))
    trials = _validate_trials(raw.get("trials"))
    assignments = _extract_assignments(members, trials)

    return TeamPreset(
        base_seed=base_seed,
        shard_stride=shard_stride,
        index_width=index_width,
        strategy=strategy,
        ranges=ranges,
        scene=scene,
        tasks=tasks,
        members=members,
        preset_hash=_canonical_hash(raw),
        trials=MappingProxyType(dict(trials)),
        member_assignments=MappingProxyType({k: tuple(v) for k, v in assignments.items()}),
    )


def submit_member_claim(
    preset: TeamPreset,
    *,
    member_id: str,
    queue_root: Path,
    ledger_path: Path,
    template_path: Path,
) -> tuple[SubmitResult, ...]:
    """Dispatch every assignment for a member into queue claims.

    For each assignment in `preset.member_assignments[member_id]` (kept in
    yaml declaration order), resolves the trial spec (task_type + fixed
    target) and delegates to submit_team_claim with the spec injected into
    cfg.training.collection.fixed_target. Returns one SubmitResult per
    assignment, in the same order.
    """
    assignments = preset.member_assignments.get(member_id)
    if not assignments:
        raise PresetError(f"Member has no assignment: {member_id}")
    results: list[SubmitResult] = []
    for a in assignments:
        trial_spec = preset.trials.get(a.trial_id)
        if trial_spec is None:
            raise PresetError(f"Assignment references unknown trial: {a.trial_id}")
        result = submit_team_claim(
            preset,
            member_id=member_id,
            task_type=trial_spec.task_type,
            queue_root=queue_root,
            ledger_path=ledger_path,
            template_path=template_path,
            trial_spec=trial_spec,
            requested_count=a.count,
        )
        results.append(result)
    return tuple(results)


def _cli_reconcile(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    queue_root = Path(args.queue_root)
    entries = reconcile_ledger_with_queue(ledger_path, queue_root)
    for entry in entries:
        member_id = entry.get("member_id")
        task_type = entry.get("task_type")
        count = entry.get("count")
        failed = entry.get("failed_indices") or []
        validated = entry.get("validated_count")
        print(
            f"{member_id}/{task_type}: {count} written, "
            f"{len(failed)} failed, {validated} validated"
        )
    return 0


def _cli_reconcile_score(args: argparse.Namespace) -> int:
    entries = reconcile_with_score_threshold(
        Path(args.ledger),
        Path(args.output_root),
        threshold=float(args.threshold),
    )
    for entry in entries:
        member_id = entry.get("member_id")
        task_type = entry.get("task_type")
        count = entry.get("count")
        low = entry.get("low_score_indices") or []
        high = entry.get("high_score_indices") or []
        missing = entry.get("missing_indices") or []
        print(
            f"{member_id}/{task_type}: {count} claimed, "
            f"{len(high)} validated (>= {args.threshold}), "
            f"{len(low)} low-score, {len(missing)} missing"
        )
    return 0


def _cli_requeue_low_score(args: argparse.Namespace) -> int:
    preset = load_preset(Path(args.preset))
    if preset is None:
        print(f"[error] preset missing: {args.preset}", file=sys.stderr)
        return 2
    try:
        results = requeue_low_score_for_member(
            preset,
            member_id=args.member,
            queue_root=Path(args.queue_root),
            ledger_path=Path(args.ledger),
            template_path=Path(args.template),
        )
    except SlotExhausted as exc:
        print(f"[error] slot exhausted: {exc}", file=sys.stderr)
        return 1
    except PresetError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    if not results:
        print(f"member={args.member}: nothing to requeue (no low-score indices)")
        return 0
    for r in results:
        print(
            f"requeued: member={args.member} "
            f"start_index={r.start_index} written={r.written_count}"
        )
    return 0


def _cli_aggregate_manifests(args: argparse.Namespace) -> int:
    from aic_collector.automation.round_helpers import aggregate_manifests

    paths = [Path(p) for p in args.manifest]
    rollup = aggregate_manifests(paths)
    print(f"manifests scanned: {len(paths)}")
    print(f"unique items:      {len(rollup['items'])}")
    print("state counts:")
    for state, count in sorted(rollup["state_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {state:>22}: {count}")
    if rollup["failures"]:
        print(f"failures ({len(rollup['failures'])}):")
        for event in rollup["failures"][:20]:
            print(f"  {event.get('item_id')} -> {event.get('state')}")
        if len(rollup["failures"]) > 20:
            print(f"  ... and {len(rollup['failures']) - 20} more")
    return 0


def _cli_verify_repo(args: argparse.Namespace) -> int:
    try:
        from huggingface_hub import HfApi  # type: ignore
    except ImportError:
        print("[error] huggingface_hub not installed", file=sys.stderr)
        return 2
    from aic_collector.automation.round_helpers import verify_repo_against_ledger

    api = HfApi()
    report = verify_repo_against_ledger(
        api=api,
        repo_id=args.repo_id,
        ledger_path=Path(args.ledger),
        repo_type=args.repo_type,
        min_files_per_item=args.min_files_per_item,
    )
    print(
        f"repo={report['repo_id']} ok={report['ok']} "
        f"min_files_per_item={report['min_files_per_item']}"
    )
    if report["ledger_errors"]:
        print("ledger errors:")
        for err in report["ledger_errors"]:
            print(f"  - {err}")
    for task, stats in report["tasks"].items():
        print(
            f"  {task}: expected={stats['expected']} present={stats['present']} "
            f"missing={len(stats['missing'])} extra={len(stats['extra'])} "
            f"below_min={len(stats['below_min_indices'])}"
        )
        if stats["missing"]:
            preview = stats["missing"][:10]
            tail = "" if len(stats["missing"]) <= 10 else f" ... (+{len(stats['missing']) - 10} more)"
            print(f"    missing indices: {preview}{tail}")
        if stats["below_min_indices"]:
            preview = stats["below_min_indices"][:10]
            tail = ("" if len(stats["below_min_indices"]) <= 10
                    else f" ... (+{len(stats['below_min_indices']) - 10} more)")
            print(f"    below-min indices: {preview}{tail}")
    return 0 if report["ok"] else 1


def _cli_retry_uploads(args: argparse.Namespace) -> int:
    try:
        from huggingface_hub import HfApi  # type: ignore
    except ImportError:
        print("[error] huggingface_hub not installed", file=sys.stderr)
        return 2
    from aic_collector.automation.round_helpers import retry_failed_uploads

    api = HfApi()
    report = retry_failed_uploads(
        manifest_path=Path(args.manifest),
        repo_id=args.repo_id,
        api=api,
        path_prefix=args.path_prefix,
        max_attempts=args.max_attempts,
        backoff_seconds=args.backoff_seconds,
    )
    if not report:
        print("nothing to retry")
        return 0
    succeeded = sum(1 for r in report if r["ok"])
    failed = len(report) - succeeded
    print(f"retried: {len(report)}  succeeded: {succeeded}  still-failed: {failed}")
    for r in report:
        flag = "OK" if r["ok"] else ("MISS" if r["missing_folder"] else "FAIL")
        line = (
            f"  [{flag}] {r['item_id']} prev={r['previous_state']} attempts={r['attempts']}"
        )
        if r["error"]:
            line += f" err={r['error']}"
        print(line)
    return 0 if failed == 0 else 1


def _cli_submit_member(args: argparse.Namespace) -> int:
    preset = load_preset(Path(args.preset))
    if preset is None:
        print(f"[error] preset missing: {args.preset}", file=sys.stderr)
        return 2
    try:
        results = submit_member_claim(
            preset,
            member_id=args.member,
            queue_root=Path(args.queue_root),
            ledger_path=Path(args.ledger),
            template_path=Path(args.template),
        )
    except SlotExhausted as exc:
        print(f"[error] slot exhausted: {exc}", file=sys.stderr)
        return 1
    except PresetError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    assignments = preset.member_assignments[args.member]
    for a, result in zip(assignments, results):
        trial = preset.trials[a.trial_id]
        print(
            f"submitted: member={args.member} trial={a.trial_id} "
            f"task={trial.task_type} fixed_target=(rail={trial.rail},port={trial.port}) "
            f"start_index={result.start_index} written={result.written_count}"
        )
    return 0


def _cli_submit(args: argparse.Namespace) -> int:
    preset = load_preset(Path(args.preset))
    if preset is None:
        print(f"[error] preset missing: {args.preset}", file=sys.stderr)
        return 2
    try:
        result = submit_team_claim(
            preset,
            member_id=args.member,
            task_type=args.task_type,
            queue_root=Path(args.queue_root),
            ledger_path=Path(args.ledger),
            template_path=Path(args.template),
        )
    except SlotExhausted as exc:
        print(f"[error] slot exhausted: {exc}", file=sys.stderr)
        return 1
    except PresetError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(
        f"submitted: member={args.member} task={args.task_type} "
        f"start_index={result.start_index} written={result.written_count}"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aic_collector.team_preset")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("reconcile", help="Annotate ledger with sim-stage failures")
    rec.add_argument("--ledger", required=True)
    rec.add_argument("--queue-root", required=True)
    rec.set_defaults(func=_cli_reconcile)

    rec_score = sub.add_parser(
        "reconcile-score",
        help="Annotate ledger with trial total < threshold indices",
    )
    rec_score.add_argument("--ledger", required=True)
    rec_score.add_argument("--output-root", required=True)
    rec_score.add_argument("--threshold", type=float, default=95.0)
    rec_score.set_defaults(func=_cli_reconcile_score)

    requeue = sub.add_parser(
        "requeue-low-score",
        help="Submit replacement configs for a member's low-score indices",
    )
    requeue.add_argument("--preset", required=True)
    requeue.add_argument("--ledger", required=True)
    requeue.add_argument("--queue-root", required=True)
    requeue.add_argument("--template", required=True)
    requeue.add_argument("--member", required=True)
    requeue.set_defaults(func=_cli_requeue_low_score)

    sub_submit = sub.add_parser("submit", help="Append a claim and write configs")
    sub_submit.add_argument("--preset", required=True)
    sub_submit.add_argument("--ledger", required=True)
    sub_submit.add_argument("--queue-root", required=True)
    sub_submit.add_argument("--template", required=True)
    sub_submit.add_argument("--member", required=True)
    sub_submit.add_argument("--task-type", required=True, choices=("sfp", "sc"))
    sub_submit.set_defaults(func=_cli_submit)

    sub_member = sub.add_parser(
        "submit-member",
        help="Append a claim using the member's preset assignment (trial + count)",
    )
    sub_member.add_argument("--preset", required=True)
    sub_member.add_argument("--ledger", required=True)
    sub_member.add_argument("--queue-root", required=True)
    sub_member.add_argument("--template", required=True)
    sub_member.add_argument("--member", required=True)
    sub_member.set_defaults(func=_cli_submit_member)

    agg = sub.add_parser(
        "aggregate-manifests",
        help="Roll up several PCs' manifest.jsonl files into one summary",
    )
    agg.add_argument("--manifest", action="append", required=True,
                     help="Path to a manifest.jsonl. Pass --manifest multiple times.")
    agg.set_defaults(func=_cli_aggregate_manifests)

    verify = sub.add_parser(
        "verify-repo",
        help="Cross-check the HF dataset repo's file inventory against the ledger",
    )
    verify.add_argument("--ledger", required=True)
    verify.add_argument("--repo-id", required=True)
    verify.add_argument("--repo-type", default="dataset")
    verify.add_argument(
        "--min-files-per-item", type=int, default=1,
        help="Minimum file count per sample_index for it to count as present. "
             "Raise to your expected per-item artifact count to gate on "
             "artifact completeness (default 1: any-file-present)",
    )
    verify.set_defaults(func=_cli_verify_repo)

    retry = sub.add_parser(
        "retry-uploads",
        help="Re-issue uploads for manifest items in a retryable failure state",
    )
    retry.add_argument("--manifest", required=True)
    retry.add_argument("--repo-id", required=True)
    retry.add_argument("--path-prefix", default="")
    retry.add_argument("--max-attempts", type=int, default=3)
    retry.add_argument("--backoff-seconds", type=float, default=2.0)
    retry.set_defaults(func=_cli_retry_uploads)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
