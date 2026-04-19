from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml


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
    ranges: dict[str, Any]
    scene: dict[str, Any]
    tasks: dict[str, int]
    members: list[dict[str, str]]
    preset_hash: str


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


def load_preset(path: Path) -> TeamPreset | None:
    if not path.exists():
        return None

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PresetError(f"Malformed preset YAML: {path}") from exc

    if not isinstance(raw, dict):
        raise PresetError("Preset root must be a mapping")

    base_seed = _require_path(raw, "team.base_seed")
    shard_stride = _require_path(raw, "team.shard_stride")
    index_width = _require_path(raw, "team.index_width")
    strategy = _require_path(raw, "sampling.strategy")
    ranges = _require_path(raw, "sampling.ranges")
    scene = _require_path(raw, "scene")
    tasks = _require_path(raw, "tasks")
    members = _require_path(raw, "members")

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
