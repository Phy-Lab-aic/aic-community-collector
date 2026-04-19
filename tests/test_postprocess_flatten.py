"""postprocess_run.process_run의 flatten 모드 검증.

큐 모드에서는 trial이 정확히 1개이므로 run_dir 바로 아래에 bag/episode/tags.json이
배치되어야 한다(trial_N_scoreNNN/ 래퍼 없음).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from aic_collector.postprocess_run import process_run


def _make_scoring(trials: dict[str, int]) -> dict:
    """trial_<N>: 점수(total) 를 받아 간단한 scoring.yaml dict 생성."""
    out: dict = {}
    for key, score in trials.items():
        out[key] = {
            "tier_1": {"score": score / 3, "message": "ok"},
            "tier_2": {"score": score / 3, "message": "ok",
                       "categories": {"duration": {"message": "Task duration: 12.3 seconds"}}},
            "tier_3": {"score": score / 3, "message": "Task successful" if score >= 50 else "failed"},
        }
    return out


def _make_engine_config(trial_keys: list[str]) -> dict:
    """엔진 config의 trials dict만 필요(실행 순서용)."""
    trials = {}
    for k in trial_keys:
        trials[k] = {"tasks": {"task_1": {"cable_type": "sfp", "plug_type": "lc", "port_type": "sfp_port_0"}}}
    return {"trials": trials}


def _make_bag_dir(parent: Path, trial_num: int) -> Path:
    """~/aic_results/bag_trial_<N>_<ts>/ 모방."""
    bag = parent / f"bag_trial_{trial_num}_20260419_120000"
    bag.mkdir(parents=True)
    (bag / "fake.mcap").write_bytes(b"X" * 2048)  # 1KB 초과
    (bag / "metadata.yaml").write_text(
        "rosbag2_bagfile_information:\n  duration:\n    nanoseconds: 12345000000\n"
    )
    return bag


def _setup_engine_results(
    tmp_path: Path,
    trials: dict[str, int],
    trial_keys_order: list[str],
) -> tuple[Path, Path, Path]:
    """엔진 결과·config 파일을 tmp_path 아래에 배치하고 경로들을 반환."""
    engine_results = tmp_path / "aic_results"
    engine_results.mkdir()
    scoring = _make_scoring(trials)
    (engine_results / "scoring.yaml").write_text(yaml.safe_dump(scoring))
    for key in trials:
        n = int(key.split("_")[1])
        _make_bag_dir(engine_results, n)

    engine_cfg = tmp_path / "engine_config.yaml"
    engine_cfg.write_text(yaml.safe_dump(_make_engine_config(trial_keys_order)))

    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()

    return engine_results, engine_cfg, demo_dir


def test_flatten_single_trial_no_trial_wrapper(tmp_path: Path) -> None:
    engine_results, engine_cfg, demo_dir = _setup_engine_results(
        tmp_path, trials={"trial_1": 97}, trial_keys_order=["trial_1"],
    )

    run_dir = tmp_path / "run_20260419_120000_sfp_0000"
    rc = process_run(
        run_dir=run_dir,
        engine_results=engine_results,
        demo_dir=demo_dir,
        engine_config=engine_cfg,
        policy="cheatcode",
        seed=42,
        parameters={"x": 1.0},
        flatten=True,
    )
    assert rc == 0

    # trial 래퍼가 없어야 한다
    assert not any(run_dir.glob("trial_*_score*")), "flat 모드에 trial 디렉토리가 생기면 안 됨"

    # 핵심 산출물이 run_dir 바로 아래에
    assert (run_dir / "tags.json").exists()
    assert (run_dir / "trial_scoring.yaml").exists()  # 평탄 모드 전용 이름
    assert (run_dir / "bag").is_dir()
    assert (run_dir / "bag" / "fake.mcap").exists()

    # 메타 파일들도 그대로
    assert (run_dir / "config.yaml").exists()
    assert (run_dir / "policy.txt").exists()
    assert (run_dir / "seed.txt").exists()
    assert (run_dir / "scoring_run.yaml").exists()

    # tags.json 내용 확인
    tags = json.loads((run_dir / "tags.json").read_text())
    assert tags["trial"] == 1
    assert tags["success"] is True
    assert tags["policy"] == "cheatcode"
    assert tags["seed"] == 42


def test_flatten_multi_trial_falls_back_to_wrapper(tmp_path: Path) -> None:
    """flatten=True지만 trial이 여러 개면 래퍼를 유지해야 안전."""
    engine_results, engine_cfg, demo_dir = _setup_engine_results(
        tmp_path,
        trials={"trial_1": 90, "trial_2": 80},
        trial_keys_order=["trial_1", "trial_2"],
    )

    run_dir = tmp_path / "run_20260419_120000"
    rc = process_run(
        run_dir=run_dir,
        engine_results=engine_results,
        demo_dir=demo_dir,
        engine_config=engine_cfg,
        policy="cheatcode",
        seed=42,
        parameters={},
        flatten=True,  # 요청됐으나 trial 2개라 무시
    )
    assert rc == 0

    # 래퍼가 생긴 상태여야 한다
    wrappers = sorted(run_dir.glob("trial_*_score*"))
    assert len(wrappers) == 2
    for w in wrappers:
        assert (w / "tags.json").exists()
        assert (w / "scoring.yaml").exists()


def test_non_flatten_default_behavior(tmp_path: Path) -> None:
    """flatten=False (기본)에서는 trial 1개여도 래퍼를 생성해야 한다(기존 동작 유지)."""
    engine_results, engine_cfg, demo_dir = _setup_engine_results(
        tmp_path, trials={"trial_1": 97}, trial_keys_order=["trial_1"],
    )

    run_dir = tmp_path / "run_01_20260419_120000"
    rc = process_run(
        run_dir=run_dir,
        engine_results=engine_results,
        demo_dir=demo_dir,
        engine_config=engine_cfg,
        policy="cheatcode",
        seed=42,
        parameters={},
        # flatten 생략 → False
    )
    assert rc == 0

    wrappers = sorted(run_dir.glob("trial_*_score*"))
    assert len(wrappers) == 1
    wrapper = wrappers[0]
    assert (wrapper / "tags.json").exists()
    assert (wrapper / "scoring.yaml").exists()
    assert not (run_dir / "tags.json").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
