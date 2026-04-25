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
    _enforce_repro_gates,
    _env_flag,
    adjust_claim_count,
    append_claim,
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


def _make_submit_fixture(tmp_path: Path) -> tuple[TeamPreset, Path, Path, Path]:
    """Build a minimal valid preset + queue/ledger/template paths."""
    preset_path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 42, shard_stride: 1000, index_width: 6}
sampling:
  strategy: lhs
  ranges:
    nic_translation: [-0.0215, 0.0234]
    nic_yaw:         [-0.1745, 0.1745]
    sc_translation:  [-0.06, 0.055]
    gripper_xy:      0.002
    gripper_z:       0.002
    gripper_rpy:     0.04
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  true
tasks: {sfp_default_count: 10, sc_default_count: 0, sfp: 10}
members:
  - {id: M0, name: alice}
  - {id: M1, name: bob}
""".strip(),
    )
    preset = load_preset(preset_path)
    assert preset is not None

    queue_root = tmp_path / "train"
    queue_root.mkdir()
    ledger_path = tmp_path / "ledger.yaml"
    template_path = tmp_path / "template.yaml"
    template_path.write_text("scoring:\n  topics: []\n", encoding="utf-8")
    return preset, queue_root, ledger_path, template_path


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


def test_load_preset_rejects_target_cycling_false_without_fixed_target(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 1, shard_stride: 10, index_width: 4}
sampling:
  strategy: uniform
  ranges:
    nic_translation: [-0.01, 0.01]
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  false
tasks: {sfp_default_count: 1, sc_default_count: 0}
members:
  - {id: M0, name: alice}
""".strip(),
    )
    with pytest.raises(PresetError, match="scene.target_cycling"):
        load_preset(path)


def test_load_preset_rejects_target_cycling_false_with_all_null_fixed_target(tmp_path: Path) -> None:
    """fixed_target present but every task is null is operationally identical to unset."""
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 1, shard_stride: 10, index_width: 4}
sampling:
  strategy: uniform
  ranges:
    nic_translation: [-0.01, 0.01]
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  false
  fixed_target:
    sfp: null
    sc: null
tasks: {sfp_default_count: 1, sc_default_count: 0}
members:
  - {id: M0, name: alice}
""".strip(),
    )
    with pytest.raises(PresetError, match="scene.target_cycling"):
        load_preset(path)


def test_load_preset_rejects_target_cycling_false_with_explicit_null_fixed_target(tmp_path: Path) -> None:
    """`fixed_target: null` must be treated the same as a missing key."""
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 1, shard_stride: 10, index_width: 4}
sampling:
  strategy: uniform
  ranges:
    nic_translation: [-0.01, 0.01]
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  false
  fixed_target:    null
tasks: {sfp_default_count: 1, sc_default_count: 0}
members:
  - {id: M0, name: alice}
""".strip(),
    )
    with pytest.raises(PresetError, match="scene.target_cycling"):
        load_preset(path)


def test_load_preset_allows_target_cycling_false_when_fixed_target_present(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 1, shard_stride: 10, index_width: 4}
sampling:
  strategy: uniform
  ranges:
    nic_translation: [-0.01, 0.01]
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  false
  fixed_target:
    sfp: {rail: 0, port: sfp_port_0}
    sc: null
tasks: {sfp_default_count: 1, sc_default_count: 0}
members:
  - {id: M0, name: alice}
""".strip(),
    )
    preset = load_preset(path)
    assert preset is not None


def test_load_preset_allows_target_cycling_true(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 1, shard_stride: 10, index_width: 4}
sampling:
  strategy: lhs
  ranges:
    nic_translation: [-0.01, 0.01]
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  true
tasks: {sfp_default_count: 1, sc_default_count: 0}
members:
  - {id: M0, name: alice}
""".strip(),
    )
    preset = load_preset(path)
    assert preset is not None


def test_env_flag_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "true", "TRUE", "yes", "Yes"):
        monkeypatch.setenv("AIC_TEST_FLAG", value)
        assert _env_flag("AIC_TEST_FLAG") is True


def test_env_flag_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("AIC_TEST_FLAG", value)
        assert _env_flag("AIC_TEST_FLAG") is False
    monkeypatch.delenv("AIC_TEST_FLAG", raising=False)
    assert _env_flag("AIC_TEST_FLAG") is False


def test_enforce_repro_gates_rejects_dirty_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_DIRTY", raising=False)
    with pytest.raises(PresetError, match="dirty tree"):
        _enforce_repro_gates(
            entries=[],
            task_type="sfp",
            preset_hash="sha256:abc",
            git_sha="dirty:abcdef",
        )


def test_enforce_repro_gates_allows_dirty_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    _enforce_repro_gates(
        entries=[],
        task_type="sfp",
        preset_hash="sha256:abc",
        git_sha="dirty:abcdef",
    )


def test_enforce_repro_gates_passes_clean_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_DIRTY", raising=False)
    _enforce_repro_gates(
        entries=[],
        task_type="sfp",
        preset_hash="sha256:abc",
        git_sha="abcdef",
    )


def test_enforce_repro_gates_allows_uncommitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_DIRTY", raising=False)
    _enforce_repro_gates(
        entries=[],
        task_type="sfp",
        preset_hash="sha256:abc",
        git_sha="uncommitted",
    )


def test_enforce_repro_gates_rejects_preset_hash_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_PRESET_DRIFT", raising=False)
    entries = [{"task_type": "sfp", "preset_hash": "sha256:old"}]
    with pytest.raises(PresetError, match="preset_hash drift"):
        _enforce_repro_gates(
            entries=entries,
            task_type="sfp",
            preset_hash="sha256:new",
            git_sha="abcdef",
        )


def test_enforce_repro_gates_allows_drift_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIC_ALLOW_PRESET_DRIFT", "1")
    entries = [{"task_type": "sfp", "preset_hash": "sha256:old"}]
    _enforce_repro_gates(
        entries=entries,
        task_type="sfp",
        preset_hash="sha256:new",
        git_sha="abcdef",
    )


def test_enforce_repro_gates_drift_isolated_per_task_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_PRESET_DRIFT", raising=False)
    entries = [{"task_type": "sfp", "preset_hash": "sha256:sfphash"}]
    # SC submission with different hash should pass — drift is checked per task_type.
    _enforce_repro_gates(
        entries=entries,
        task_type="sc",
        preset_hash="sha256:schash",
        git_sha="abcdef",
    )


def test_enforce_repro_gates_first_submit_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_PRESET_DRIFT", raising=False)
    _enforce_repro_gates(
        entries=[],
        task_type="sfp",
        preset_hash="sha256:any",
        git_sha="abcdef",
    )


def test_submit_team_claim_blocks_on_dirty_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aic_collector import team_preset as tp_mod

    monkeypatch.delenv("AIC_ALLOW_DIRTY", raising=False)
    monkeypatch.setattr(tp_mod, "_git_sha", lambda: "dirty:cafef00d")

    preset, queue_root, ledger_path, template_path = _make_submit_fixture(tmp_path)

    with pytest.raises(PresetError, match="dirty tree"):
        tp_mod.submit_team_claim(
            preset,
            member_id="M0",
            task_type="sfp",
            queue_root=queue_root,
            ledger_path=ledger_path,
            template_path=template_path,
        )
    # Ledger must be untouched.
    assert not ledger_path.exists() or _load_ledger(ledger_path) == {"entries": []}


def test_submit_team_claim_blocks_on_preset_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aic_collector import team_preset as tp_mod

    monkeypatch.delenv("AIC_ALLOW_PRESET_DRIFT", raising=False)
    monkeypatch.setattr(tp_mod, "_git_sha", lambda: "cleanabc")

    preset, queue_root, ledger_path, template_path = _make_submit_fixture(tmp_path)

    # Seed an entry with a different preset_hash.
    seeded = [
        {
            "member_id": "M0",
            "task_type": "sfp",
            "base_seed": preset.base_seed,
            "start_index": 0,
            "count": 0,
            "strategy": preset.strategy,
            "queue_root": str(queue_root),
            "preset_hash": "sha256:olddifferent",
            "git_sha": "cleanabc",
            "created_at": "2026-04-20T00:00:00Z",
        }
    ]
    yaml.safe_dump({"entries": seeded}, ledger_path.open("w"), sort_keys=False)

    with pytest.raises(PresetError, match="preset_hash drift"):
        tp_mod.submit_team_claim(
            preset,
            member_id="M1",
            task_type="sfp",
            queue_root=queue_root,
            ledger_path=ledger_path,
            template_path=template_path,
        )
    # Only the seeded entry survives.
    assert len(_load_ledger(ledger_path)["entries"]) == 1
