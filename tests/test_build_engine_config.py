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
