from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.team_preset import PresetError, TeamPreset, load_preset


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
  - name: alpha
    role: lead
  - name: beta
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
    assert preset.members == [
        {"name": "alpha", "role": "lead"},
        {"name": "beta", "role": "support"},
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
  - name: alpha
    role: lead
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
  - name: alpha
    role: lead
  - name: beta
    role: support
""".strip(),
    )
    path_b = _write_preset(
        tmp_path / "preset_b.yaml",
        """
members:
  - role: lead
    name: alpha
  - role: support
    name: beta
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
