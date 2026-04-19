from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml

from aic_collector.job_queue import QueueState, legacy_dir, queue_dir


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


_CONFIG_INDEX_RE_TEMPLATE = r"config_{task_type}_(\d+)\.yaml"


def _canonical_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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
        members.append(MappingProxyType(normalized))
    return tuple(members)


def _member_index(preset: TeamPreset, member_id: str) -> int:
    for index, member in enumerate(preset.members):
        if member.get("id") == member_id:
            return index
    raise KeyError(member_id)


def slot_range(preset: TeamPreset, member_id: str) -> tuple[int, int]:
    member_index = _member_index(preset, member_id)
    slot_start = member_index * preset.shard_stride
    return slot_start, slot_start + preset.shard_stride


def next_start_index_in_slot(
    preset: TeamPreset, member_id: str, queue_root: Path, task_type: str
) -> int:
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

    if highest_index is None:
        return slot_start
    return highest_index + 1


def load_preset(path: Path) -> TeamPreset | None:
    if not path.exists():
        return None

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PresetError(f"Malformed preset YAML: {path}") from exc

    if not isinstance(raw, dict):
        raise PresetError("Preset root must be a mapping")

    base_seed = _validate_int(_require_path(raw, "team.base_seed"), "team.base_seed")
    shard_stride = _validate_int(_require_path(raw, "team.shard_stride"), "team.shard_stride")
    index_width = _validate_int(_require_path(raw, "team.index_width"), "team.index_width")
    strategy = _validate_strategy(_require_path(raw, "sampling.strategy"))
    ranges = _freeze(_validate_mapping(_require_path(raw, "sampling.ranges"), "sampling.ranges"))
    scene = _freeze(_validate_mapping(_require_path(raw, "scene"), "scene"))
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
