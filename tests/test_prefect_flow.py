from __future__ import annotations

from pathlib import Path


def test_restart_docker_removes_previous_engine_results_without_backup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aic_collector.prefect import flow as flow_mod

    engine_results = tmp_path / "aic_results"
    engine_results.mkdir()
    (engine_results / "old.txt").write_text("stale result", encoding="utf-8")

    monkeypatch.setattr(flow_mod, "ENGINE_RESULTS", engine_results)
    monkeypatch.setattr(flow_mod, "READY_FLAG", tmp_path / "aic_ready")
    monkeypatch.setattr(flow_mod, "DONE_FLAG", tmp_path / "aic_done")
    monkeypatch.setattr(flow_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("USER", "tester")

    calls: list[list[str]] = []

    def fake_run_shell_process(
        cmd: list[str],
        *,
        log_path: str,
        env: dict[str, str],
    ) -> tuple[int, str]:
        calls.append(cmd)
        return (0, "")

    monkeypatch.setattr(flow_mod, "run_shell_process", fake_run_shell_process)

    flow_mod.restart_docker_task.fn(container="aic_eval")

    assert not engine_results.exists()
    assert list(tmp_path.glob("aic_results_e2e_backup_*")) == []
    assert calls == [
        ["docker", "exec", "aic_eval", "id", "tester"],
        ["docker", "restart", "aic_eval"],
    ]
