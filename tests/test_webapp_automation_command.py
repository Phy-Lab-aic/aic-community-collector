from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.webapp import build_automation_runner_command  # noqa: E402


def test_build_automation_runner_command_uses_isolated_tmp_paths(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "output"

    spec = build_automation_runner_command(
        batch_size=7,
        hf_repo_id="org/dataset",
        queue_root=queue_root,
        output_root=output_root,
    )

    cmd = spec.command
    assert cmd[:3] == ["uv", "run", "aic-automation-batch"]
    assert cmd[cmd.index("--batch-size") + 1] == "7"
    assert cmd[cmd.index("--hf-repo-id") + 1] == "org/dataset"
    assert cmd[cmd.index("--queue-root") + 1] == str(queue_root)
    assert cmd[cmd.index("--output-root") + 1] == str(output_root)
    assert cmd[cmd.index("--pid-file") + 1] == "/tmp/aic_automation_pid.txt"
    assert cmd[cmd.index("--status-file") + 1] == "/tmp/aic_automation_status.json"
    assert cmd[cmd.index("--log-file") + 1] == "/tmp/aic_automation_run.log"
    assert cmd[cmd.index("--worker-state-file") + 1] == "/tmp/aic_automation_worker_state.json"
    assert spec.env["AIC_WORKER_STATE_FILE"] == "/tmp/aic_automation_worker_state.json"
    assert spec.worker_state_file != Path("/tmp/aic_worker_state.json")
