"""Tests for trial-sharded preset extensions (TrialSpec, MemberAssignment, submit_member_claim)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.team_preset import (  # noqa: E402
    MemberAssignment,
    PresetError,
    TrialSpec,
    load_preset,
    reconcile_with_score_threshold,
    requeue_low_score_for_member,
    submit_member_claim,
)


_PRESET_TRIALS = """
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
tasks: {sfp_default_count: 0, sc_default_count: 0}
trials:
  trial_1:
    task_type: sfp
    fixed_target: {rail: 0, port: sfp_port_0}
  trial_2:
    task_type: sfp
    fixed_target: {rail: 1, port: sfp_port_0}
  trial_3:
    task_type: sc
    fixed_target: {rail: 1, port: sc_port_1}
members:
  - {id: M0, name: alice, assignment: {trial: trial_1, count: 4}}
  - {id: M1, name: bob,   assignment: {trial: trial_2, count: 4}}
  - {id: M2, name: carol, assignment: {trial: trial_3, count: 2}}
  - {id: M3, name: dave}
""".strip()


def _write_template(path: Path) -> Path:
    template = path / "template.yaml"
    template.write_text(
        "scoring:\n  topics: []\ntask_board_limits: {}\nrobot: {}\n",
        encoding="utf-8",
    )
    return template


def _make_round(tmp_path: Path, preset_text: str = _PRESET_TRIALS):
    preset_path = tmp_path / "preset.yaml"
    preset_path.write_text(preset_text, encoding="utf-8")
    preset = load_preset(preset_path)
    assert preset is not None
    queue_root = tmp_path / "train"
    queue_root.mkdir()
    ledger = tmp_path / "ledger.yaml"
    template = _write_template(tmp_path)
    return preset, queue_root, ledger, template


def test_load_preset_parses_trials_and_assignments(tmp_path: Path) -> None:
    preset, *_ = _make_round(tmp_path)

    assert set(preset.trials) == {"trial_1", "trial_2", "trial_3"}
    assert preset.trials["trial_1"] == TrialSpec(
        trial_id="trial_1", task_type="sfp", rail=0, port="sfp_port_0"
    )
    assert preset.trials["trial_3"] == TrialSpec(
        trial_id="trial_3", task_type="sc", rail=1, port="sc_port_1"
    )
    assert preset.member_assignments == {
        "M0": (MemberAssignment(trial_id="trial_1", count=4),),
        "M1": (MemberAssignment(trial_id="trial_2", count=4),),
        "M2": (MemberAssignment(trial_id="trial_3", count=2),),
    }


def test_load_preset_rejects_assignment_referencing_unknown_trial(tmp_path: Path) -> None:
    bad = _PRESET_TRIALS.replace("trial: trial_1", "trial: trial_99", 1)
    path = tmp_path / "preset.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(PresetError, match="trial_99"):
        load_preset(path)


def test_load_preset_rejects_invalid_port(tmp_path: Path) -> None:
    bad = _PRESET_TRIALS.replace("port: sfp_port_0", "port: bogus", 1)
    path = tmp_path / "preset.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(PresetError, match="port"):
        load_preset(path)


def test_load_preset_rejects_non_positive_count(tmp_path: Path) -> None:
    bad = _PRESET_TRIALS.replace("count: 4", "count: 0", 1)
    path = tmp_path / "preset.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(PresetError, match="positive"):
        load_preset(path)


def test_submit_member_claim_writes_trial1_sfp_configs_with_rail0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    preset, queue_root, ledger, template = _make_round(tmp_path)

    results = submit_member_claim(
        preset,
        member_id="M0",
        queue_root=queue_root,
        ledger_path=ledger,
        template_path=template,
    )

    assert len(results) == 1
    result = results[0]
    assert result.written_count == 4
    assert result.start_index == 0  # M0 slot starts at 0

    sfp_pending = sorted((queue_root / "sfp" / "pending").glob("config_sfp_*.yaml"))
    assert len(sfp_pending) == 4

    # Every SFP config must target NIC card 0 + sfp_port_0 (trial_1 fixed_target).
    for cfg_path in sfp_pending:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        task = cfg["trials"]["trial_1"]["tasks"]["task_1"]
        assert task["target_module_name"] == "nic_card_mount_0"
        assert task["port_name"] == "sfp_port_0"

    # Ledger entry annotated with trial_id + fixed_target
    entries = yaml.safe_load(ledger.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 1
    assert entries[0]["trial_id"] == "trial_1"
    assert entries[0]["fixed_target"] == {"rail": 0, "port": "sfp_port_0"}
    assert entries[0]["task_type"] == "sfp"


def test_submit_member_claim_dispatches_trial3_to_sc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    preset, queue_root, ledger, template = _make_round(tmp_path)

    results = submit_member_claim(
        preset,
        member_id="M2",
        queue_root=queue_root,
        ledger_path=ledger,
        template_path=template,
    )

    assert len(results) == 1
    result = results[0]
    assert result.written_count == 2
    sc_pending = sorted((queue_root / "sc" / "pending").glob("config_sc_*.yaml"))
    assert len(sc_pending) == 2

    for cfg_path in sc_pending:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        task = cfg["trials"]["trial_1"]["tasks"]["task_1"]
        assert task["target_module_name"] == "sc_port_1"
        assert task["port_name"] == "sc_port_base"
        assert task["plug_type"] == "sc"


def test_submit_member_claim_uses_member_slot_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    preset, queue_root, ledger, template = _make_round(tmp_path)

    submit_member_claim(
        preset, member_id="M0", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )
    results_m1 = submit_member_claim(
        preset, member_id="M1", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )

    # M1 slot starts at shard_stride (1000).
    assert results_m1[0].start_index == 1000
    indices = sorted(
        int(p.stem.rsplit("_", 1)[-1])
        for p in (queue_root / "sfp" / "pending").glob("config_sfp_*.yaml")
    )
    assert indices[:4] == [0, 1, 2, 3]
    assert indices[4:] == [1000, 1001, 1002, 1003]


def test_submit_member_claim_without_assignment_raises(tmp_path: Path) -> None:
    preset, queue_root, ledger, template = _make_round(tmp_path)
    with pytest.raises(PresetError, match="assignment"):
        submit_member_claim(
            preset, member_id="M3",  # M3 has no assignment block
            queue_root=queue_root, ledger_path=ledger, template_path=template,
        )


def _write_run(
    output_root: Path,
    *,
    timestamp: str,
    task: str,
    sample_index: int,
    trial_num: int,
    tier_scores: tuple[float, float, float],
) -> Path:
    """Build a minimal run_<ts>_<task>_<idx>/scoring_run.yaml fixture."""
    run_dir = output_root / f"run_{timestamp}_{task}_{sample_index:04d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    scoring = {
        f"trial_{trial_num}": {
            "tier_1": {"score": tier_scores[0]},
            "tier_2": {"score": tier_scores[1]},
            "tier_3": {"score": tier_scores[2]},
        }
    }
    (run_dir / "scoring_run.yaml").write_text(
        yaml.safe_dump(scoring, sort_keys=False), encoding="utf-8"
    )
    return run_dir


def test_reconcile_with_score_threshold_partitions_indices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    preset, queue_root, ledger, template = _make_round(tmp_path)
    submit_member_claim(
        preset, member_id="M0", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )

    output_root = tmp_path / "out"
    output_root.mkdir()
    # 4 indices claimed: 0,1,2,3. Score them: 100, 80, 95, no run (missing).
    _write_run(output_root, timestamp="20260507_010000", task="sfp",
               sample_index=0, trial_num=1, tier_scores=(50, 30, 20))
    _write_run(output_root, timestamp="20260507_010001", task="sfp",
               sample_index=1, trial_num=1, tier_scores=(40, 30, 10))
    _write_run(output_root, timestamp="20260507_010002", task="sfp",
               sample_index=2, trial_num=1, tier_scores=(50, 30, 15))
    # index 3: no run dir written

    entries = reconcile_with_score_threshold(ledger, output_root, threshold=95.0)
    assert len(entries) == 1
    e = entries[0]
    assert e["high_score_indices"] == [0, 2]
    assert e["low_score_indices"] == [1]
    assert e["missing_indices"] == [3]
    assert e["score_validated_count"] == 2
    assert e["score_threshold"] == 95.0
    assert "score_reconciled_at" in e


def test_reconcile_score_keeps_latest_run_per_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    preset, queue_root, ledger, template = _make_round(tmp_path)
    submit_member_claim(
        preset, member_id="M0", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )

    output_root = tmp_path / "out"
    output_root.mkdir()
    # First attempt: low score
    _write_run(output_root, timestamp="20260507_010000", task="sfp",
               sample_index=0, trial_num=1, tier_scores=(50, 30, 10))
    # Re-collected later: high score (lexicographically later timestamp wins)
    _write_run(output_root, timestamp="20260507_020000", task="sfp",
               sample_index=0, trial_num=1, tier_scores=(50, 30, 20))

    entries = reconcile_with_score_threshold(ledger, output_root, threshold=95.0)
    assert entries[0]["high_score_indices"] == [0]
    assert entries[0]["low_score_indices"] == []


def test_reconcile_score_ignores_other_task_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    preset, queue_root, ledger, template = _make_round(tmp_path)
    submit_member_claim(  # M2 -> SC, count=2
        preset, member_id="M2", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )

    output_root = tmp_path / "out"
    output_root.mkdir()
    # SC index 2000 (slot offset = 2 * 1000) -> high; 2001 -> low
    _write_run(output_root, timestamp="20260507_010000", task="sc",
               sample_index=2000, trial_num=1, tier_scores=(50, 30, 20))
    _write_run(output_root, timestamp="20260507_010001", task="sc",
               sample_index=2001, trial_num=1, tier_scores=(40, 30, 10))
    # An unrelated SFP run shouldn't influence the SC entry
    _write_run(output_root, timestamp="20260507_010002", task="sfp",
               sample_index=2000, trial_num=1, tier_scores=(0, 0, 0))

    entries = reconcile_with_score_threshold(ledger, output_root, threshold=95.0)
    assert entries[0]["high_score_indices"] == [2000]
    assert entries[0]["low_score_indices"] == [2001]


def test_requeue_low_score_writes_new_configs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    preset, queue_root, ledger, template = _make_round(tmp_path)
    submit_member_claim(
        preset, member_id="M0", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )

    output_root = tmp_path / "out"
    output_root.mkdir()
    # 2 of 4 indices fall below 95
    _write_run(output_root, timestamp="20260507_010000", task="sfp",
               sample_index=0, trial_num=1, tier_scores=(50, 30, 20))  # 100
    _write_run(output_root, timestamp="20260507_010001", task="sfp",
               sample_index=1, trial_num=1, tier_scores=(30, 20, 10))  # 60
    _write_run(output_root, timestamp="20260507_010002", task="sfp",
               sample_index=2, trial_num=1, tier_scores=(50, 30, 15))  # 95
    _write_run(output_root, timestamp="20260507_010003", task="sfp",
               sample_index=3, trial_num=1, tier_scores=(20, 10, 5))   # 35

    reconcile_with_score_threshold(ledger, output_root, threshold=95.0)
    results = requeue_low_score_for_member(
        preset, member_id="M0", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )
    assert len(results) == 1
    assert results[0].written_count == 2  # indices 1 and 3 were below 95
    # Original 4 (0..3) + replacement 2 (4..5) = 6 pending configs.
    pending = sorted((queue_root / "sfp" / "pending").glob("config_sfp_*.yaml"))
    indices = sorted(int(p.stem.rsplit("_", 1)[-1]) for p in pending)
    assert indices == [0, 1, 2, 3, 4, 5]


def test_requeue_low_score_returns_none_when_all_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    preset, queue_root, ledger, template = _make_round(tmp_path)
    submit_member_claim(
        preset, member_id="M0", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )
    output_root = tmp_path / "out"
    output_root.mkdir()
    for i in range(4):
        _write_run(output_root, timestamp=f"2026050{i+1}_010000", task="sfp",
                   sample_index=i, trial_num=1, tier_scores=(50, 30, 20))

    reconcile_with_score_threshold(ledger, output_root, threshold=95.0)
    results = requeue_low_score_for_member(
        preset, member_id="M0", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )
    assert results == ()


_PRESET_MULTI = """
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
tasks: {sfp_default_count: 0, sc_default_count: 0}
trials:
  trial_1:
    task_type: sfp
    fixed_target: {rail: 0, port: sfp_port_0}
  trial_2:
    task_type: sfp
    fixed_target: {rail: 1, port: sfp_port_0}
members:
  - id: M0
    name: alice
    assignments:
      - {trial: trial_1, count: 3}
      - {trial: trial_2, count: 2}
""".strip()


def test_submit_member_claim_dispatches_each_assignment_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    preset, queue_root, ledger, template = _make_round(tmp_path, _PRESET_MULTI)

    results = submit_member_claim(
        preset, member_id="M0", queue_root=queue_root,
        ledger_path=ledger, template_path=template,
    )

    assert len(results) == 2
    # Both assignments are SFP, so they share the same task slot.
    # First (trial_1) gets indices 0..2; second (trial_2) gets 3..4.
    assert results[0].start_index == 0
    assert results[0].written_count == 3
    assert results[1].start_index == 3
    assert results[1].written_count == 2

    # Inspect ledger to ensure both trial_ids were recorded.
    entries = yaml.safe_load(ledger.read_text(encoding="utf-8"))["entries"]
    assert [e["trial_id"] for e in entries] == ["trial_1", "trial_2"]
    assert entries[0]["fixed_target"] == {"rail": 0, "port": "sfp_port_0"}
    assert entries[1]["fixed_target"] == {"rail": 1, "port": "sfp_port_0"}


def test_load_preset_rejects_both_assignment_and_assignments(tmp_path: Path) -> None:
    bad = _PRESET_MULTI.replace(
        "    assignments:\n",
        "    assignment: {trial: trial_1, count: 1}\n    assignments:\n",
    )
    path = tmp_path / "preset.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(PresetError, match="either"):
        load_preset(path)


def test_load_preset_rejects_duplicate_trial_in_assignments(tmp_path: Path) -> None:
    bad = _PRESET_MULTI.replace(
        "      - {trial: trial_2, count: 2}",
        "      - {trial: trial_1, count: 2}",
    )
    path = tmp_path / "preset.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(PresetError, match="duplicate trial"):
        load_preset(path)


def test_load_preset_without_trials_keeps_existing_callers_working(
    tmp_path: Path,
) -> None:
    """Backwards compat: preset without `trials` block should still load."""
    text = """
team: {base_seed: 42, shard_stride: 1000, index_width: 6}
sampling:
  strategy: lhs
  ranges: {nic_translation: [-0.02, 0.02]}
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  true
tasks: {sfp: 5, sc: 5}
members:
  - {id: M0, name: alice}
""".strip()
    path = tmp_path / "preset.yaml"
    path.write_text(text, encoding="utf-8")
    preset = load_preset(path)
    assert preset is not None
    assert preset.trials == {}
    assert preset.member_assignments == {}
