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
