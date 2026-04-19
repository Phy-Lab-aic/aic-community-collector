from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import ANY

import pytest
import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.job_queue import QueueState, queue_dir, write_plan
from aic_collector.team_preset import SlotExhausted, SubmitResult, TeamPreset, submit_team_claim

TEMPLATE_PATH = PROJECT_DIR / "configs/community_random_config.yaml"


def _preset(
    *,
    shard_stride: int = 100_000,
    scene: dict[str, object] | None = None,
    tasks: dict[str, int] | None = None,
) -> TeamPreset:
    return TeamPreset(
        base_seed=100,
        shard_stride=shard_stride,
        index_width=6,
        strategy="uniform",
        ranges={},
        scene=scene or {},
        tasks=tasks or {"sfp": 3},
        members=(
            {"id": "m0", "name": "Member 0"},
            {"id": "m1", "name": "Member 1"},
        ),
        preset_hash="sha256:test",
    )


def _ledger_entries(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if payload is None:
        return []
    return list(payload["entries"])


def _pending_configs(root: Path, task_type: str) -> list[Path]:
    return sorted(queue_dir(root, task_type, QueueState.PENDING).glob("*.yaml"))


def _config(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_submit_team_claim_happy_path_writes_three_files_and_one_ledger_entry(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    ledger_path = tmp_path / "ledger.yaml"

    result = submit_team_claim(
        _preset(),
        member_id="m0",
        task_type="sfp",
        queue_root=queue_root,
        ledger_path=ledger_path,
        template_path=TEMPLATE_PATH,
    )

    assert result == SubmitResult(start_index=0, written_count=3, entry_id=0)
    assert [path.name for path in _pending_configs(queue_root, "sfp")] == [
        "config_sfp_000000.yaml",
        "config_sfp_000001.yaml",
        "config_sfp_000002.yaml",
    ]
    assert _ledger_entries(ledger_path) == [
        {
            "member_id": "m0",
            "task_type": "sfp",
            "base_seed": 100,
            "start_index": 0,
            "count": 3,
            "strategy": "uniform",
            "queue_root": str(queue_root),
            "preset_hash": "sha256:test",
            "git_sha": ANY,
            "created_at": ANY,
        }
    ]


def test_submit_team_claim_second_submit_for_same_member_uses_next_index(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    ledger_path = tmp_path / "ledger.yaml"
    preset = _preset()

    first = submit_team_claim(
        preset,
        member_id="m0",
        task_type="sfp",
        queue_root=queue_root,
        ledger_path=ledger_path,
        template_path=TEMPLATE_PATH,
    )
    second = submit_team_claim(
        preset,
        member_id="m0",
        task_type="sfp",
        queue_root=queue_root,
        ledger_path=ledger_path,
        template_path=TEMPLATE_PATH,
    )

    assert first.start_index == 0
    assert second.start_index == 3
    assert [entry["start_index"] for entry in _ledger_entries(ledger_path)] == [0, 3]
    assert [path.name for path in _pending_configs(queue_root, "sfp")] == [
        "config_sfp_000000.yaml",
        "config_sfp_000001.yaml",
        "config_sfp_000002.yaml",
        "config_sfp_000003.yaml",
        "config_sfp_000004.yaml",
        "config_sfp_000005.yaml",
    ]


def test_submit_team_claim_different_members_get_disjoint_start_indices(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    ledger_path = tmp_path / "ledger.yaml"
    preset = _preset(shard_stride=100_000)

    first = submit_team_claim(
        preset,
        member_id="m0",
        task_type="sfp",
        queue_root=queue_root,
        ledger_path=ledger_path,
        template_path=TEMPLATE_PATH,
    )
    second = submit_team_claim(
        preset,
        member_id="m1",
        task_type="sfp",
        queue_root=queue_root,
        ledger_path=ledger_path,
        template_path=TEMPLATE_PATH,
    )

    assert first.start_index == 0
    assert second.start_index == 100_000
    assert [entry["start_index"] for entry in _ledger_entries(ledger_path)] == [0, 100_000]


def test_submit_team_claim_slot_exhaustion_leaves_ledger_and_files_untouched(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    ledger_path = tmp_path / "ledger.yaml"

    with pytest.raises(SlotExhausted):
        submit_team_claim(
            _preset(shard_stride=3, tasks={"sfp": 4}),
            member_id="m0",
            task_type="sfp",
            queue_root=queue_root,
            ledger_path=ledger_path,
            template_path=TEMPLATE_PATH,
        )

    assert _ledger_entries(ledger_path) == []
    assert _pending_configs(queue_root, "sfp") == []


def test_submit_team_claim_sampling_failure_rolls_back_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue_root = tmp_path / "queue"
    ledger_path = tmp_path / "ledger.yaml"

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("sampling failed")

    monkeypatch.setattr("aic_collector.team_preset.sample_scenes", boom)

    with pytest.raises(RuntimeError, match="sampling failed"):
        submit_team_claim(
            _preset(),
            member_id="m0",
            task_type="sfp",
            queue_root=queue_root,
            ledger_path=ledger_path,
            template_path=TEMPLATE_PATH,
        )

    assert _ledger_entries(ledger_path) == []
    assert _pending_configs(queue_root, "sfp") == []


def test_submit_team_claim_partial_write_failure_adjusts_ledger_count_for_written_range_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_root = tmp_path / "queue"
    ledger_path = tmp_path / "ledger.yaml"

    def partial_write(plans: list[object], root: Path, template_path: Path, index_width: int = 6) -> list[Path]:
        written = [
            write_plan(plans[0], root, template_path, index_width=index_width),
            write_plan(plans[1], root, template_path, index_width=index_width),
        ]
        pending_dir = queue_dir(root, "sfp", QueueState.PENDING)
        pending_dir.mkdir(parents=True, exist_ok=True)
        (pending_dir / "config_sfp_999999.yaml").write_text("unrelated: true\n", encoding="utf-8")
        raise RuntimeError("disk full")

    monkeypatch.setattr("aic_collector.team_preset.write_plans", partial_write)

    with pytest.raises(RuntimeError, match="disk full"):
        submit_team_claim(
            _preset(tasks={"sfp": 3}),
            member_id="m0",
            task_type="sfp",
            queue_root=queue_root,
            ledger_path=ledger_path,
            template_path=TEMPLATE_PATH,
        )

    assert [entry["count"] for entry in _ledger_entries(ledger_path)] == [2]
    assert [path.name for path in _pending_configs(queue_root, "sfp")] == [
        "config_sfp_000000.yaml",
        "config_sfp_000001.yaml",
        "config_sfp_999999.yaml",
    ]


def test_submit_team_claim_fixed_target_is_reflected_in_produced_configs(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    ledger_path = tmp_path / "ledger.yaml"
    preset = _preset(
        scene={"fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}}},
        tasks={"sfp": 2},
    )

    submit_team_claim(
        preset,
        member_id="m0",
        task_type="sfp",
        queue_root=queue_root,
        ledger_path=ledger_path,
        template_path=TEMPLATE_PATH,
    )

    produced = [_config(path) for path in _pending_configs(queue_root, "sfp")]
    assert len(produced) == 2
    assert {
        (
            cfg["trials"]["trial_1"]["tasks"]["task_1"]["target_module_name"],
            cfg["trials"]["trial_1"]["tasks"]["task_1"]["port_name"],
        )
        for cfg in produced
    } == {("nic_card_mount_0", "sfp_port_0")}
