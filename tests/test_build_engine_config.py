from pathlib import Path

import yaml

from aic_collector.build_engine_config import build


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_build_preserves_scoring_storage_options() -> None:
    cfg_text = build(
        PROJECT_DIR / "configs/community_random_config.yaml",
        ["1"],
        {
            "nic0_translation": 0.0,
            "nic0_yaw": 0.0,
            "nic1_translation": 0.0,
            "nic1_yaw": 0.0,
            "sc0_translation": 0.0,
            "sc0_yaw": 0.0,
            "sc1_translation": 0.0,
            "sc1_yaw": 0.0,
        },
    )

    cfg = yaml.safe_load(cfg_text)

    assert cfg["scoring"]["storage_id"] == "mcap"
    assert cfg["scoring"]["storage_preset_profile"] == "zstd_fast"


def test_low_spec_20hz_config_keeps_raw_headless_minimum() -> None:
    cfg = yaml.safe_load((PROJECT_DIR / "configs/e2e_low_spec_20hz.yaml").read_text())

    assert cfg["collection"]["collect_episode"] is False
    assert cfg["engine"]["headless"] is True
    assert cfg["engine"]["use_compressed"] is False
    assert "target_hz" not in cfg["engine"]
