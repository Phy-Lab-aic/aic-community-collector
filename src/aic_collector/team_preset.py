from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
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
class TeamPreset:
    base_seed: int
    shard_stride: int
    index_width: int
    strategy: Literal["uniform", "lhs"]
    ranges: Mapping[str, Any]
    scene: Mapping[str, Any]
    tasks: Mapping[str, int]
    members: tuple[Mapping[str, str], ...]
    preset_hash: str


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


def _validate_members(value: Any) -> tuple[Mapping[str, str], ...]:
    if not isinstance(value, list):
        raise PresetError("Invalid members field: members")

    members: list[Mapping[str, str]] = []
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

        normalized = {str(k): str(v) for k, v in member.items()}
        member_id = normalized["id"]
        if member_id in member_ids:
            raise PresetError(f"Invalid members field: duplicate member id: {member_id}")
        member_ids.add(member_id)
        members.append(MappingProxyType(normalized))
    return tuple(members)


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


def _training_cfg_from_preset(preset: TeamPreset) -> dict[str, Any]:
    scene_cfg = _thaw(preset.scene)
    collection_cfg: dict[str, Any] = {}
    fixed_target = scene_cfg.pop("fixed_target", None)
    if fixed_target is not None:
        collection_cfg["fixed_target"] = _thaw(fixed_target)

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
    """
    with _ledger_lock(ledger_path):
        entries = _ledger_entries(ledger_path)

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
) -> SubmitResult:
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
        _write_ledger_entries(ledger_path, entries)

        try:
            plans = sample_scenes(
                _training_cfg_from_preset(preset),
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
    )
