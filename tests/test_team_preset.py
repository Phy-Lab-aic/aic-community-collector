from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from unittest.mock import ANY

import pytest
import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.job_queue import QueueState, legacy_dir, queue_dir  # noqa: E402
from aic_collector.team_preset import (  # noqa: E402
    PresetError,
    SlotExhausted,
    TeamPreset,
    adjust_claim_count,
    append_claim,
    load_presets,
    load_preset,
    next_start_index_in_slot,
    rollback_claim,
    slot_range,
)


def _write_preset(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _load_ledger(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


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


def test_load_presets_reads_sorted_catalog_and_campaign_fields(tmp_path: Path) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()

    trial_2_path = _write_preset(
        preset_dir / "trial_2.yaml",
        """
version: 1
campaign:
  trial_id: trial_2
  task_type: sfp
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: {}
scene:
  env: training
  fixed_target:
    sfp:
      rail: 1
      port: sfp_port_0
    sc: null
members:
  - id: alpha
    name: Alpha
""".strip(),
    )
    _write_preset(
        preset_dir / "trial_1.yaml",
        trial_2_path.read_text(encoding="utf-8").replace("trial_2", "trial_1").replace(
            "rail: 1", "rail: 0"
        ),
    )

    presets, issues = load_presets(preset_dir)

    assert [preset.preset_name for preset in presets] == ["trial_1", "trial_2"]
    assert issues == ()
    assert presets[0].trial_id == "trial_1"
    assert presets[0].task_type == "sfp"
    assert presets[0].total_target_count == 1000
    assert presets[0].batch_default_count == 100
    assert presets[0].is_catalog_preset is True


def test_load_presets_reports_invalid_catalog_files(tmp_path: Path) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    _write_preset(
        preset_dir / "bad.yaml",
        """
version: 1
campaign:
  trial_id: trial_2
  task_type: sfp
  total_target_count: 100
  batch_default_count: 200
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: {}
scene:
  env: training
  fixed_target:
    sfp:
      rail: 1
      port: sfp_port_0
    sc: null
members:
  - id: alpha
    name: Alpha
""".strip(),
    )

    presets, issues = load_presets(preset_dir)

    assert presets == ()
    assert len(issues) == 1
    assert issues[0].path.name == "bad.yaml"
    assert "batch_default_count" in issues[0].message


@pytest.mark.parametrize(
    ("name", "scene_block", "message_fragment"),
    [
        (
            "missing_fixed_target.yaml",
            """
scene:
  env: training
""".strip(),
            "scene.fixed_target",
        ),
        (
            "missing_active_branch.yaml",
            """
scene:
  env: training
  fixed_target:
    sfp: null
    sc:
      rail: 1
      port: sc_port_1
""".strip(),
            "scene.fixed_target.sfp",
        ),
        (
            "malformed_active_branch.yaml",
            """
scene:
  env: training
  fixed_target:
    sfp:
      rail: 1
    sc: null
""".strip(),
            "scene.fixed_target.sfp",
        ),
    ],
)
def test_load_presets_rejects_invalid_active_fixed_target_shapes(
    tmp_path: Path, name: str, scene_block: str, message_fragment: str
) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    _write_preset(
        preset_dir / name,
        f"""
version: 1
campaign:
  trial_id: trial_2
  task_type: sfp
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: {{}}
{scene_block}
members:
  - id: alpha
    name: Alpha
""".strip(),
    )

    presets, issues = load_presets(preset_dir)

    assert presets == ()
    assert len(issues) == 1
    assert issues[0].path.name == name
    assert message_fragment in issues[0].message


def test_load_presets_rejects_malformed_inactive_fixed_target_branch(tmp_path: Path) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    _write_preset(
        preset_dir / "bad.yaml",
        """
version: 1
campaign:
  trial_id: trial_2
  task_type: sfp
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: {}
scene:
  env: training
  fixed_target:
    sfp:
      rail: 1
      port: sfp_port_0
    sc: "bad"
members:
  - id: alpha
    name: Alpha
""".strip(),
    )

    presets, issues = load_presets(preset_dir)

    assert presets == ()
    assert len(issues) == 1
    assert issues[0].path.name == "bad.yaml"
    assert "scene.fixed_target.sc" in issues[0].message


def test_load_presets_rejects_unexpected_fixed_target_key(tmp_path: Path) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    _write_preset(
        preset_dir / "bad.yaml",
        """
version: 1
campaign:
  trial_id: trial_2
  task_type: sfp
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: {}
scene:
  env: training
  fixed_target:
    sfp:
      rail: 1
      port: sfp_port_0
    sc: null
    extra:
      rail: 0
      port: extra_port
members:
  - id: alpha
    name: Alpha
""".strip(),
    )

    presets, issues = load_presets(preset_dir)

    assert presets == ()
    assert len(issues) == 1
    assert issues[0].path.name == "bad.yaml"
    assert "scene.fixed_target.extra" in issues[0].message


@pytest.mark.parametrize(
    ("trial_id", "task_type", "scene_block"),
    [
        (
            "trial_2",
            "sfp",
            """
scene:
  env: training
  fixed_target:
    sfp:
      rail: 1
      port: sfp_port_0
    sc: null
""".strip(),
        ),
        (
            "trial_3",
            "sc",
            """
scene:
  env: training
  fixed_target:
    sfp: null
    sc:
      rail: 1
      port: sc_port_1
""".strip(),
        ),
    ],
)
def test_load_presets_accepts_valid_inactive_null_and_active_present_fixed_targets(
    tmp_path: Path, trial_id: str, task_type: str, scene_block: str
) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    _write_preset(
        preset_dir / f"{trial_id}.yaml",
        f"""
version: 1
campaign:
  trial_id: {trial_id}
  task_type: {task_type}
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 100
  shard_stride: 17
  index_width: 4
sampling:
  strategy: uniform
  ranges: {{}}
{scene_block}
members:
  - id: alpha
    name: Alpha
""".strip(),
    )

    presets, issues = load_presets(preset_dir)

    assert [preset.preset_name for preset in presets] == [trial_id]
    assert issues == ()
    assert presets[0].trial_id == trial_id
    assert presets[0].task_type == task_type
    assert presets[0].is_catalog_preset is True


def test_load_preset_legacy_schema_still_works(tmp_path: Path) -> None:
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
    assert preset.is_catalog_preset is False
    assert preset.trial_id is None
    assert preset.task_type is None


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


def test_load_preset_rejects_duplicate_member_ids(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
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
  - id: alpha
    name: Alpha
  - id: alpha
    name: Alpha Clone
""".strip(),
    )

    with pytest.raises(PresetError, match="duplicate member id"):
        load_preset(path)


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("team.shard_stride", 0),
        ("team.shard_stride", -1),
        ("team.index_width", 0),
        ("team.index_width", -1),
        ("team.base_seed", -1),
    ],
)
def test_load_preset_rejects_non_positive_team_numeric_settings(
    tmp_path: Path,
    field_name: str,
    field_value: int,
) -> None:
    base_seed = field_value if field_name == "team.base_seed" else 100
    shard_stride = field_value if field_name == "team.shard_stride" else 17
    index_width = field_value if field_name == "team.index_width" else 4
    path = _write_preset(
        tmp_path / f"{field_name.replace('.', '_')}.yaml",
        f"""
team:
  base_seed: {base_seed}
  shard_stride: {shard_stride}
  index_width: {index_width}
sampling:
  strategy: uniform
  ranges: {{}}
scene: {{}}
tasks:
  sfp: 12
members:
  - id: alpha
    name: Alpha
""".strip(),
    )

    with pytest.raises(PresetError, match=re.escape(field_name)):
        load_preset(path)


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


def test_next_start_index_in_slot_raises_when_slot_is_full(tmp_path: Path) -> None:
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

    failed_dir = queue_dir(tmp_path, "sfp", QueueState.FAILED)
    failed_dir.mkdir(parents=True)
    (failed_dir / "config_sfp_0019.yaml").write_text("x", encoding="utf-8")

    with pytest.raises(SlotExhausted, match="beta"):
        next_start_index_in_slot(preset, "beta", tmp_path, "sfp")


def test_append_claim_writes_one_entry_with_required_fields(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.yaml"

    entry_id = append_claim(
        ledger_path,
        member_id="alpha",
        task_type="sfp",
        base_seed=100,
        start_index=17,
        count=4,
        strategy="uniform",
        queue_root=tmp_path / "queue",
        preset_hash="sha256:preset",
    )

    ledger = _load_ledger(ledger_path)

    assert entry_id == 0
    assert ledger["entries"] == [
        {
            "member_id": "alpha",
            "task_type": "sfp",
            "base_seed": 100,
            "start_index": 17,
            "count": 4,
            "strategy": "uniform",
            "queue_root": str(tmp_path / "queue"),
            "preset_hash": "sha256:preset",
            "git_sha": ANY,
            "created_at": ANY,
        }
    ]
    assert isinstance(ledger["entries"][0]["git_sha"], str)
    assert ledger["entries"][0]["created_at"].endswith("Z")


def test_second_append_gets_next_id_and_preserves_existing_entries(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.yaml"

    first_id = append_claim(
        ledger_path,
        member_id="alpha",
        task_type="sfp",
        base_seed=100,
        start_index=0,
        count=2,
        strategy="uniform",
        queue_root=tmp_path / "queue-a",
        preset_hash="sha256:first",
    )
    second_id = append_claim(
        ledger_path,
        member_id="beta",
        task_type="sc",
        base_seed=200,
        start_index=10,
        count=3,
        strategy="lhs",
        queue_root=tmp_path / "queue-b",
        preset_hash="sha256:second",
    )

    ledger = _load_ledger(ledger_path)

    assert first_id == 0
    assert second_id == 1
    assert [entry["member_id"] for entry in ledger["entries"]] == ["alpha", "beta"]
    assert [entry["start_index"] for entry in ledger["entries"]] == [0, 10]


def test_rollback_claim_removes_only_the_last_entry(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.yaml"
    append_claim(
        ledger_path,
        member_id="alpha",
        task_type="sfp",
        base_seed=100,
        start_index=0,
        count=2,
        strategy="uniform",
        queue_root=tmp_path / "queue-a",
        preset_hash="sha256:first",
    )
    append_claim(
        ledger_path,
        member_id="beta",
        task_type="sc",
        base_seed=200,
        start_index=10,
        count=3,
        strategy="lhs",
        queue_root=tmp_path / "queue-b",
        preset_hash="sha256:second",
    )

    rollback_claim(ledger_path, 0)
    after_noop = _load_ledger(ledger_path)
    rollback_claim(ledger_path, 1)
    after_pop = _load_ledger(ledger_path)

    assert [entry["member_id"] for entry in after_noop["entries"]] == ["alpha", "beta"]
    assert [entry["member_id"] for entry in after_pop["entries"]] == ["alpha"]


def test_adjust_claim_count_updates_only_count_field(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.yaml"
    entry_id = append_claim(
        ledger_path,
        member_id="alpha",
        task_type="sfp",
        base_seed=100,
        start_index=17,
        count=4,
        strategy="uniform",
        queue_root=tmp_path / "queue",
        preset_hash="sha256:preset",
    )
    before = _load_ledger(ledger_path)["entries"][0].copy()

    adjust_claim_count(ledger_path, entry_id, 9)

    after = _load_ledger(ledger_path)["entries"][0]

    assert after["count"] == 9
    assert {key: value for key, value in after.items() if key != "count"} == {
        key: value for key, value in before.items() if key != "count"
    }


def test_adjust_claim_count_rejects_negative_entry_id(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.yaml"
    append_claim(
        ledger_path,
        member_id="alpha",
        task_type="sfp",
        base_seed=100,
        start_index=0,
        count=2,
        strategy="uniform",
        queue_root=tmp_path / "queue-a",
        preset_hash="sha256:first",
    )
    append_claim(
        ledger_path,
        member_id="beta",
        task_type="sfp",
        base_seed=101,
        start_index=10,
        count=3,
        strategy="uniform",
        queue_root=tmp_path / "queue-b",
        preset_hash="sha256:second",
    )

    with pytest.raises(PresetError, match="Invalid ledger entry id"):
        adjust_claim_count(ledger_path, -1, 99)

    ledger = _load_ledger(ledger_path)

    assert [entry["count"] for entry in ledger["entries"]] == [2, 3]


def test_rollback_claim_rejects_negative_entry_id(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.yaml"
    append_claim(
        ledger_path,
        member_id="alpha",
        task_type="sfp",
        base_seed=100,
        start_index=0,
        count=2,
        strategy="uniform",
        queue_root=tmp_path / "queue-a",
        preset_hash="sha256:first",
    )
    append_claim(
        ledger_path,
        member_id="beta",
        task_type="sfp",
        base_seed=101,
        start_index=10,
        count=3,
        strategy="uniform",
        queue_root=tmp_path / "queue-b",
        preset_hash="sha256:second",
    )

    with pytest.raises(PresetError, match="Invalid ledger entry id"):
        rollback_claim(ledger_path, -1)

    ledger = _load_ledger(ledger_path)

    assert [entry["member_id"] for entry in ledger["entries"]] == ["alpha", "beta"]


def test_concurrent_append_claims_preserve_all_entries(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.yaml"
    entry_ids: list[int] = []
    lock = threading.Lock()

    def append_for(index: int) -> None:
        entry_id = append_claim(
            ledger_path,
            member_id=f"member-{index}",
            task_type="sfp",
            base_seed=100 + index,
            start_index=index * 10,
            count=index + 1,
            strategy="uniform",
            queue_root=tmp_path / f"queue-{index}",
            preset_hash=f"sha256:{index}",
        )
        with lock:
            entry_ids.append(entry_id)

    threads = [threading.Thread(target=append_for, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ledger = _load_ledger(ledger_path)

    assert sorted(entry_ids) == list(range(8))
    assert len(ledger["entries"]) == 8
    assert sorted(entry["member_id"] for entry in ledger["entries"]) == [
        f"member-{index}" for index in range(8)
    ]
