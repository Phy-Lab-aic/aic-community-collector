from __future__ import annotations

from pathlib import Path

from aic_collector.webapp import (
    AUTOMATION_LOG_FILE,
    AUTOMATION_PID_FILE,
    AUTOMATION_STATE_FILE,
    build_automation_command,
    build_automation_env,
)


def test_automation_command_uses_isolated_tmp_files_and_worker_state(tmp_path: Path) -> None:
    cmd = build_automation_command(
        batch_size=2,
        hf_repo_id="org/repo",
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "out",
        staging_root=tmp_path / "stage",
        manifest_path=tmp_path / "manifest.jsonl",
        converter_path=tmp_path / "third_party/rosbag-to-lerobot",
        repeat_count=1,
    )

    assert cmd[:3] == ["uv", "run", "aic-automation-batch"]
    assert cmd[cmd.index("--batch-size") + 1] == "2"
    assert cmd[cmd.index("--hf-repo-id") + 1] == "org/repo"
    assert cmd[cmd.index("--worker-state-file") + 1] == str(AUTOMATION_STATE_FILE)
    assert str(AUTOMATION_PID_FILE) not in cmd
    assert str(AUTOMATION_LOG_FILE) not in cmd


def test_automation_env_does_not_persist_hf_token(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "secret")
    env = build_automation_env()

    assert env["AIC_WORKER_STATE_FILE"] == str(AUTOMATION_STATE_FILE)
    assert env["HF_TOKEN"] == "secret"
    assert "AIC_HF_TOKEN" not in env
