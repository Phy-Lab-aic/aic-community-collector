"""webapp.load_results가 legacy(trial 래퍼)와 flat 구조를 모두 읽는지 검증."""

from __future__ import annotations

import json
from pathlib import Path

from aic_collector.webapp import load_results


def _write_tags(path: Path, **kwargs) -> None:
    base = {
        "schema_version": "0.1.0",
        "trial": 1,
        "success": True,
        "scoring": {"total": 97.0, "tier_3_message": "Task successful"},
        "policy": "cheatcode",
        "seed": 42,
        "trial_duration_sec": 12.5,
    }
    base.update(kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base))


def test_load_results_flat(tmp_path: Path) -> None:
    run = tmp_path / "run_20260419_120000_sfp_0000"
    _write_tags(run / "tags.json", trial=1, scoring={"total": 97.0, "tier_3_message": "Task successful"})

    rows = load_results(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["run"] == "run_20260419_120000_sfp_0000"
    assert r["trial"] == 1
    assert r["score"] == 97.0
    assert r["success"] == "✅"
    assert r["policy"] == "cheatcode"
    assert r["duration"] == 12.5
    assert r["time"] == "2026-04-19 12:00:00"


def test_load_results_legacy(tmp_path: Path) -> None:
    run = tmp_path / "run_01_20260413_194804"
    _write_tags(run / "trial_2_score96" / "tags.json", trial=2,
                scoring={"total": 96.0, "tier_3_message": "Task successful"})
    _write_tags(run / "trial_3_score25" / "tags.json", trial=3, success=False,
                scoring={"total": 25.0, "tier_3_message": "failed"})

    rows = load_results(tmp_path)
    assert len(rows) == 2
    trials = sorted(r["trial"] for r in rows)
    assert trials == [2, 3]


def test_load_results_mixed(tmp_path: Path) -> None:
    """같은 output_root에 legacy run과 flat run이 공존해도 모두 읽혀야 한다."""
    flat_run = tmp_path / "run_20260419_120000_sfp_0000"
    _write_tags(flat_run / "tags.json", trial=1,
                scoring={"total": 90.0, "tier_3_message": "Task successful"})

    legacy_run = tmp_path / "run_01_20260413_194804"
    _write_tags(legacy_run / "trial_2_score96" / "tags.json", trial=2,
                scoring={"total": 96.0, "tier_3_message": "Task successful"})

    rows = load_results(tmp_path)
    assert len(rows) == 2
    run_names = {r["run"] for r in rows}
    assert run_names == {flat_run.name, legacy_run.name}


def test_load_results_empty(tmp_path: Path) -> None:
    assert load_results(tmp_path) == []


def test_load_results_flat_takes_precedence_over_stray_trial_dirs(tmp_path: Path) -> None:
    """flat run_dir에 남은 trial_*_score* 잔재가 있어도 flat tags.json만 읽어야 한다."""
    run = tmp_path / "run_20260419_120000_sfp_0000"
    _write_tags(run / "tags.json", trial=1,
                scoring={"total": 80.0, "tier_3_message": "Task successful"})
    # 의도적으로 트래쉬 trial 디렉토리를 함께 둠
    _write_tags(run / "trial_9_score1" / "tags.json", trial=9, success=False,
                scoring={"total": 1.0})

    rows = load_results(tmp_path)
    assert len(rows) == 1
    assert rows[0]["trial"] == 1
    assert rows[0]["score"] == 80.0
