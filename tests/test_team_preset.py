from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.job_queue import QueueState, legacy_dir, queue_dir
from aic_collector.team_preset import (
    PresetError,
    TeamPreset,
    load_preset,
    next_start_index_in_slot,
    slot_range,
)


def _write_preset(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_load_preset_happy_path(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges:
    nic_translation:
      min: -0.02
      max: 0.02
scene:
  env: training
tasks:
  sfp: 12
  sc: 8
members:
  - id: alpha
    name: Alpha
    role: lead
  - id: beta
    name: Beta
    role: support
""".strip(),
    )

    preset = load_preset(path)

    assert isinstance(preset, TeamPreset)
    assert preset.base_seed == 100
    assert preset.shard_stride == 17
    assert preset.index_width == 4
    assert preset.strategy == "uniform"
    assert preset.ranges == {"nic_translation": {"min": -0.02, "max": 0.02}}
    assert preset.scene == {"env": "training"}
    assert preset.tasks == {"sfp": 12, "sc": 8}
    assert list(preset.members) == [
        {"id": "alpha", "name": "Alpha", "role": "lead"},
        {"id": "beta", "name": "Beta", "role": "support"},
    ]
    assert preset.preset_hash.startswith("sha256:")


def test_load_preset_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_preset(tmp_path / "missing.yaml") is None


def test_load_preset_malformed_yaml_raises_preset_error(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team:
  base_seed: [1, 2
""".strip(),
    )

    with pytest.raises(PresetError):
        load_preset(path)


def test_load_preset_missing_required_field_raises_preset_error(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team:
  base_seed: 100
  shard_stride: 17
sampling:
  strategy: uniform
  ranges: {}
scene: {}
tasks:
  sfp: 12
members:
  - id: alpha
    name: Alpha
""".strip(),
    )

    with pytest.raises(PresetError, match="team.index_width"):
        load_preset(path)


def test_load_preset_hash_stable_across_key_reordering(tmp_path: Path) -> None:
    path_a = _write_preset(
        tmp_path / "preset_a.yaml",
        """
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: lhs
  ranges:
    nic_translation:
      min: -0.02
      max: 0.02
scene:
  env: training
  version: 1
tasks:
  sfp: 12
  sc: 8
members:
  - id: alpha
    name: Alpha
    role: lead
  - id: beta
    name: Beta
    role: support
""".strip(),
    )
    path_b = _write_preset(
        tmp_path / "preset_b.yaml",
        """
members:
  - role: lead
    name: Alpha
    id: alpha
  - role: support
    name: Beta
    id: beta
tasks:
  sc: 8
  sfp: 12
scene:
  version: 1
  env: training
sampling:
  ranges:
    nic_translation:
      max: 0.02
      min: -0.02
  strategy: lhs
team:
  index_width: 4
  shard_stride: 17
  base_seed: 100
""".strip(),
    )

    preset_a = load_preset(path_a)
    preset_b = load_preset(path_b)

    assert preset_a is not None
    assert preset_b is not None
    assert preset_a.preset_hash == preset_b.preset_hash


@pytest.mark.parametrize(
    ("field_name", "content"),
    [
        (
            "team.base_seed",
            """
team:
  base_seed: not-an-int
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: {}
scene: {}
tasks:
  sfp: 12
members:
  - id: alpha
    name: Alpha
""".strip(),
        ),
        (
            "sampling.strategy",
            """
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: random
  ranges: {}
scene: {}
tasks:
  sfp: 12
members:
  - id: alpha
    name: Alpha
""".strip(),
        ),
        (
            "sampling.ranges",
            """
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: []
scene: {}
tasks:
  sfp: 12
members:
  - id: alpha
    name: Alpha
""".strip(),
        ),
        (
            "tasks",
            """
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: {}
scene: {}
tasks:
  - sfp
members:
  - id: alpha
    name: Alpha
""".strip(),
        ),
        (
            "members[0].id",
            """
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: {}
scene: {}
tasks:
  sfp: 12
members:
  - id: null
    name: Alpha
""".strip(),
        ),
    ],
)
def test_load_preset_rejects_malformed_parseable_content(
    tmp_path: Path, field_name: str, content: str
) -> None:
    path = _write_preset(tmp_path / f"{field_name.replace('.', '_')}.yaml", content)

    with pytest.raises(PresetError, match=re.escape(field_name)):
        load_preset(path)


def test_load_preset_returns_immutable_nested_data(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges:
    nic_translation:
      min: -0.02
      max: 0.02
scene:
  env: training
tasks:
  sfp: 12
members:
  - id: alpha
    name: Alpha
""".strip(),
    )

    preset = load_preset(path)

    assert preset is not None
    with pytest.raises(TypeError):
        preset.scene["env"] = "eval"
    with pytest.raises(TypeError):
        preset.ranges["nic_translation"]["min"] = -0.5
    with pytest.raises(AttributeError):
        preset.members.append({"id": "beta", "name": "Beta"})
    with pytest.raises(TypeError):
        preset.members[0]["name"] = "Changed"


def test_slot_range_uses_member_position_and_stride() -> None:
    preset = TeamPreset(
        base_seed=100,
        shard_stride=17,
        index_width=4,
        strategy="uniform",
        ranges={},
        scene={},
        tasks={},
        members=(
            {"id": "alpha", "name": "Alpha"},
            {"id": "beta", "name": "Beta"},
            {"id": "gamma", "name": "Gamma"},
        ),
        preset_hash="sha256:test",
    )

    assert slot_range(preset, "alpha") == (0, 17)
    assert slot_range(preset, "beta") == (17, 34)
    assert slot_range(preset, "gamma") == (34, 51)


def test_slot_range_unknown_member_raises_key_error() -> None:
    preset = TeamPreset(
        base_seed=100,
        shard_stride=17,
        index_width=4,
        strategy="uniform",
        ranges={},
        scene={},
        tasks={},
        members=({"id": "alpha", "name": "Alpha"},),
        preset_hash="sha256:test",
    )

    with pytest.raises(KeyError, match="missing"):
        slot_range(preset, "missing")


def test_next_start_index_in_slot_empty_returns_slot_start(tmp_path: Path) -> None:
    preset = TeamPreset(
        base_seed=100,
        shard_stride=10,
        index_width=4,
        strategy="uniform",
        ranges={},
        scene={},
        tasks={"sfp": 1},
        members=(
            {"id": "alpha", "name": "Alpha"},
            {"id": "beta", "name": "Beta"},
        ),
        preset_hash="sha256:test",
    )

    assert next_start_index_in_slot(preset, "beta", tmp_path, "sfp") == 10


def test_next_start_index_in_slot_advances_with_existing_files(tmp_path: Path) -> None:
    preset = TeamPreset(
        base_seed=100,
        shard_stride=10,
        index_width=4,
        strategy="uniform",
        ranges={},
        scene={},
        tasks={"sfp": 1},
        members=(
            {"id": "alpha", "name": "Alpha"},
            {"id": "beta", "name": "Beta"},
        ),
        preset_hash="sha256:test",
    )

    pending_dir = queue_dir(tmp_path, "sfp", QueueState.PENDING)
    done_dir = queue_dir(tmp_path, "sfp", QueueState.DONE)
    pending_dir.mkdir(parents=True)
    done_dir.mkdir(parents=True)
    (pending_dir / "config_sfp_0012.yaml").write_text("x", encoding="utf-8")
    (done_dir / "config_sfp_0018.yaml").write_text("x", encoding="utf-8")

    assert next_start_index_in_slot(preset, "beta", tmp_path, "sfp") == 19


def test_next_start_index_in_slot_ignores_other_slots(tmp_path: Path) -> None:
    preset = TeamPreset(
        base_seed=100,
        shard_stride=10,
        index_width=4,
        strategy="uniform",
        ranges={},
        scene={},
        tasks={"sfp": 1},
        members=(
            {"id": "alpha", "name": "Alpha"},
            {"id": "beta", "name": "Beta"},
        ),
        preset_hash="sha256:test",
    )

    running_dir = queue_dir(tmp_path, "sfp", QueueState.RUNNING)
    legacy = legacy_dir(tmp_path, "sfp")
    running_dir.mkdir(parents=True)
    legacy.mkdir(parents=True, exist_ok=True)
    (running_dir / "config_sfp_0009.yaml").write_text("x", encoding="utf-8")
    (legacy / "config_sfp_0027.yaml").write_text("x", encoding="utf-8")

    assert next_start_index_in_slot(preset, "beta", tmp_path, "sfp") == 10
