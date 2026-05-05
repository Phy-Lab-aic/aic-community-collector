"""consumer_cli command-line plumbing tests (no real subprocess)."""
from __future__ import annotations

from pathlib import Path

from aic_collector.job_queue import consumer_cli


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def _capture_run(monkeypatch) -> list[list[str]]:
    captured: list[list[str]] = []

    def fake_subprocess_run(cmd, *, stdout, stderr, timeout, check):
        captured.append(list(cmd))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(consumer_cli.subprocess, "run", fake_subprocess_run)
    return captured


def test_run_one_default_passes_no_headless(monkeypatch, tmp_path: Path) -> None:
    captured = _capture_run(monkeypatch)

    rc = consumer_cli.run_one(
        running_path=tmp_path / "config_sfp_0001.yaml",
        policy="cheatcode",
        act_model_path=None,
        ground_truth=True,
        use_compressed=False,
        collect_episode=False,
        output_root="~/aic_community_e2e",
        run_tag="tag",
        timeout_sec=None,
        log_path=None,
    )

    assert rc == 0
    cmd = captured[0]
    assert "--no-headless" in cmd
    assert "--headless" not in [arg for arg in cmd if arg == "--headless"]


def test_run_one_headless_propagates_flag(monkeypatch, tmp_path: Path) -> None:
    captured = _capture_run(monkeypatch)

    consumer_cli.run_one(
        running_path=tmp_path / "config_sfp_0001.yaml",
        policy="cheatcode",
        act_model_path=None,
        ground_truth=True,
        use_compressed=False,
        collect_episode=False,
        output_root="~/aic_community_e2e",
        run_tag="tag",
        timeout_sec=None,
        log_path=None,
        headless=True,
    )

    cmd = captured[0]
    assert "--headless" in cmd
    assert "--no-headless" not in cmd


def test_resolve_worker_state_file_prefers_cli_over_env(monkeypatch, tmp_path: Path) -> None:
    env_state = tmp_path / "env_state.json"
    cli_state = tmp_path / "cli_state.json"
    monkeypatch.setenv("AIC_WORKER_STATE_FILE", str(env_state))

    assert consumer_cli.resolve_worker_state_file(str(cli_state)) == cli_state


def test_main_uses_env_state_file_without_touching_default(monkeypatch, tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    default_state = tmp_path / "default_state.json"
    env_state = tmp_path / "automation_worker_state.json"
    log_path = tmp_path / "worker.log"
    monkeypatch.setattr(consumer_cli, "DEFAULT_WORKER_STATE_FILE", default_state)
    monkeypatch.setenv("AIC_WORKER_STATE_FILE", str(env_state))
    monkeypatch.setattr(
        consumer_cli.sys,
        "argv",
        [
            "aic-collector-worker",
            "--root", str(queue_root),
            "--limit", "1",
            "--log", str(log_path),
        ],
    )

    rc = consumer_cli.main()

    assert rc == 0
    assert env_state.exists()
    assert not default_state.exists()
