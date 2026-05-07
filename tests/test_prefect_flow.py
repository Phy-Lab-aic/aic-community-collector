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


def test_restart_docker_chowns_container_owned_engine_results_on_permission_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aic_collector.prefect import flow as flow_mod

    engine_results = tmp_path / "aic_results"
    engine_results.mkdir()
    (engine_results / "root_owned.txt").write_text("stale result", encoding="utf-8")

    monkeypatch.setattr(flow_mod, "ENGINE_RESULTS", engine_results)
    monkeypatch.setattr(flow_mod, "READY_FLAG", tmp_path / "aic_ready")
    monkeypatch.setattr(flow_mod, "DONE_FLAG", tmp_path / "aic_done")
    monkeypatch.setattr(flow_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setattr(flow_mod.os, "getuid", lambda: 1000)
    monkeypatch.setattr(flow_mod.os, "getgid", lambda: 1000)

    calls: list[list[str]] = []
    original_rmtree = flow_mod.shutil.rmtree
    attempts = {"count": 0}

    def flaky_rmtree(path: Path) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise PermissionError("container-owned file")
        original_rmtree(path)

    def fake_run_shell_process(
        cmd: list[str],
        *,
        log_path: str,
        env: dict[str, str],
    ) -> tuple[int, str]:
        calls.append(cmd)
        return (0, "")

    monkeypatch.setattr(flow_mod.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(flow_mod, "run_shell_process", fake_run_shell_process)

    flow_mod.restart_docker_task.fn(container="aic_eval")

    assert not engine_results.exists()
    assert attempts["count"] == 2
    assert calls == [
        ["docker", "exec", "aic_eval", "id", "tester"],
        [
            "docker",
            "exec",
            "-u",
            "root",
            "aic_eval",
            "chown",
            "-R",
            "1000:1000",
            str(engine_results),
        ],
        ["docker", "restart", "aic_eval"],
    ]


def _spy_launch_engine(monkeypatch) -> list[list[str]]:
    """Capture distrobox cmd handed to run_process_background, skip sleep."""
    from aic_collector.prefect import flow as flow_mod

    captured: list[list[str]] = []

    def fake_run_process_background(
        cmd: list[str],
        *,
        log_path: str,
        env: dict[str, str],
    ) -> int:
        captured.append(list(cmd))
        return 4242

    monkeypatch.setattr(flow_mod, "run_process_background", fake_run_process_background)
    monkeypatch.setattr(flow_mod.time, "sleep", lambda _seconds: None)
    return captured


def test_launch_engine_task_default_keeps_gui_on(monkeypatch) -> None:
    from aic_collector.prefect import flow as flow_mod

    captured = _spy_launch_engine(monkeypatch)

    handle = flow_mod.launch_engine_task.fn(
        engine_cfg="/tmp/cfg.yaml",
        ground_truth=True,
        run_tag="tag",
        run_idx=1,
        startup_wait=0,
    )

    assert handle["pid"] == 4242
    cmd = captured[0]
    assert "gazebo_gui:=false" not in cmd
    assert "launch_rviz:=false" not in cmd


def test_launch_engine_task_headless_disables_gazebo_and_rviz(monkeypatch) -> None:
    from aic_collector.prefect import flow as flow_mod

    captured = _spy_launch_engine(monkeypatch)

    flow_mod.launch_engine_task.fn(
        engine_cfg="/tmp/cfg.yaml",
        ground_truth=False,
        run_tag="tag",
        run_idx=1,
        startup_wait=0,
        headless=True,
    )

    cmd = captured[0]
    assert "gazebo_gui:=false" in cmd
    assert "launch_rviz:=false" in cmd
    assert "ground_truth:=false" in cmd
    assert "start_aic_engine:=true" in cmd
    assert "aic_engine_config_file:=/tmp/cfg.yaml" in cmd
