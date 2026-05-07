from __future__ import annotations

from pathlib import Path

from aic_collector.job_queue import consumer_cli


def test_state_file_prefers_cli_arg_over_env(monkeypatch, tmp_path: Path) -> None:
    env_state = tmp_path / "env.json"
    cli_state = tmp_path / "cli.json"
    monkeypatch.setenv("AIC_WORKER_STATE_FILE", str(env_state))

    assert consumer_cli.resolve_worker_state_file(str(cli_state)) == cli_state


def test_state_file_uses_env_when_cli_absent(monkeypatch, tmp_path: Path) -> None:
    env_state = tmp_path / "env.json"
    monkeypatch.setenv("AIC_WORKER_STATE_FILE", str(env_state))

    assert consumer_cli.resolve_worker_state_file(None) == env_state


def test_write_state_uses_selected_path(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    consumer_cli._write_state({"status": "running"}, state_file=state_file)

    assert state_file.read_text() == '{"status": "running"}'
