"""consumer_cli command-line plumbing tests (no real subprocess)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from aic_collector.job_queue import consumer_cli
from aic_collector.automation.manifest import append_event, materialize, read_events
from aic_collector.job_queue.worker import ClaimedConfig


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


def test_default_worker_batch_id_is_multi_uploader_safe(monkeypatch) -> None:
    class FakeUuid:
        hex = "abcdef1234567890"

    monkeypatch.setattr(consumer_cli.getpass, "getuser", lambda: "user/name")
    monkeypatch.setattr(consumer_cli.socket, "gethostname", lambda: "host.name")
    monkeypatch.setattr(consumer_cli.os, "getpid", lambda: 12345)
    monkeypatch.setattr(consumer_cli, "uuid4", lambda: FakeUuid())

    batch_id = consumer_cli._default_worker_batch_id(datetime(2026, 5, 6, 1, 2, 3))

    assert batch_id == "worker-20260506_010203-user-name-host-12345-abcdef12"


def test_lerobot_upload_automation_runs_inside_worker_success_path(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    run_tag = "20260505_120000_sfp_0001"
    run_dir = tmp_path / "out" / f"run_{run_tag}"
    (run_dir / "bag").mkdir(parents=True)
    (run_dir / "bag/data.mcap").write_bytes(b"mcap")
    (run_dir / "tags.json").write_text("{}")
    (run_dir / "episode").mkdir()
    (run_dir / "validation.json").write_text('{"success": true}')
    converter_path = tmp_path / "converter"
    (converter_path / "src").mkdir(parents=True)
    (converter_path / "src/main.py").write_text("# fake")

    def fake_converter(*, converter_path, input_path, output_path, config_path=None):
        output_path.mkdir(parents=True)
        (output_path / "meta.json").write_text("{}")
        return 0

    def fake_upload(**kwargs):
        append_event(
            kwargs["manifest_path"],
            item_id=kwargs["item_id"],
            state="uploaded",
            batch_id=kwargs["batch_id"],
            upload={"repo_id": kwargs["repo_id"], "revision": "abc123", "path_in_repo": kwargs["path_in_repo"]},
        )
        append_event(
            kwargs["manifest_path"],
            item_id=kwargs["item_id"],
            state="remote_verified",
            batch_id=kwargs["batch_id"],
            remote={"ok": True, "revision": "abc123"},
        )
        return {"ok": True, "revision": "abc123"}

    monkeypatch.setattr("aic_collector.automation.batch_runner.run_converter", fake_converter)
    monkeypatch.setattr("aic_collector.automation.batch_runner.record_upload_and_verify", fake_upload)

    claim = ClaimedConfig(
        task_type="sfp",
        sample_index=1,
        running_path=tmp_path / "queue/sfp/running/config_sfp_0001.yaml",
    )
    cfg = consumer_cli.LerobotUploadConfig(
        hf_repo_id="org/repo",
        manifest_path=manifest,
        staging_root=tmp_path / "stage",
        lerobot_root=tmp_path / "lerobot",
        converter_path=converter_path,
        path_prefix="worker",
        batch_id="batch-1",
    )

    consumer_cli.record_worker_manifest_start(cfg, claim)
    result = consumer_cli.run_lerobot_upload_automation(
        config=cfg,
        claim=claim,
        done_path=tmp_path / "queue/sfp/done/config_sfp_0001.yaml",
        output_root=str(tmp_path / "out"),
        run_tag=run_tag,
        collect_episode=True,
    )

    assert result["ok"] is True
    events = read_events(manifest)
    item_states = [event["state"] for event in events if event["item_id"] == "config_sfp_0001"]
    assert item_states[:9] == [
        "planned",
        "worker_started",
        "worker_finished",
        "reconciled",
        "collected_validated",
        "staged",
        "converted",
        "uploaded",
        "remote_verified",
    ]
    assert materialize(manifest)["config_sfp_0001"]["state"] == "cleanup_done"
    assert not run_dir.exists()


def test_upload_lerobot_batch_scopes_local_folder_by_batch_id(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    item_root = tmp_path / "items/config_sfp_0001"
    item_root.mkdir(parents=True)
    (item_root / "meta.json").write_text("{}")
    captured: dict[str, Path | str] = {}

    def fake_record_upload_and_verify(**kwargs):
        captured["local_folder"] = kwargs["local_folder"]
        captured["path_in_repo"] = kwargs["path_in_repo"]
        append_event(
            kwargs["manifest_path"],
            item_id=kwargs["item_id"],
            state="uploaded",
            batch_id=kwargs["batch_id"],
            upload={"path_in_repo": kwargs["path_in_repo"]},
        )
        append_event(
            kwargs["manifest_path"],
            item_id=kwargs["item_id"],
            state="remote_verified",
            batch_id=kwargs["batch_id"],
            remote={"ok": True},
        )
        return {"ok": True}

    monkeypatch.setattr("aic_collector.automation.batch_runner.record_upload_and_verify", fake_record_upload_and_verify)

    cfg = consumer_cli.LerobotUploadConfig(
        hf_repo_id="org/repo",
        manifest_path=manifest,
        staging_root=tmp_path / "stage",
        lerobot_root=tmp_path / "lerobot",
        converter_path=tmp_path / "converter",
        path_prefix="worker",
        batch_id="worker-user-host-1234-abcd",
        cleanup_after_upload=False,
    )
    item = consumer_cli.PreparedLerobotItem(
        item_id="config_sfp_0001",
        run_dir=tmp_path / "run",
        staged_path=tmp_path / "stage/config_sfp_0001",
        lerobot_path=item_root,
    )

    result = consumer_cli.upload_lerobot_batch(config=cfg, items=[item], batch_index=1)

    assert result["ok"] is True
    assert captured["local_folder"] == tmp_path / "lerobot/upload_batches/worker-user-host-1234-abcd/batch_0001"
    assert captured["path_in_repo"] == "worker/worker-user-host-1234-abcd/batch_0001"


def test_lerobot_upload_conversion_error_is_item_failure_not_worker_crash(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    run_tag = "20260505_120000_sfp_0002"
    run_dir = tmp_path / "out" / f"run_{run_tag}"
    (run_dir / "bag").mkdir(parents=True)
    (run_dir / "bag/data.mcap").write_bytes(b"mcap")
    (run_dir / "tags.json").write_text("{}")
    (run_dir / "episode").mkdir()
    (run_dir / "validation.json").write_text('{"success": true}')

    claim = ClaimedConfig(
        task_type="sfp",
        sample_index=2,
        running_path=tmp_path / "queue/sfp/running/config_sfp_0002.yaml",
    )
    cfg = consumer_cli.LerobotUploadConfig(
        hf_repo_id="org/repo",
        manifest_path=manifest,
        staging_root=tmp_path / "stage",
        lerobot_root=tmp_path / "lerobot",
        converter_path=tmp_path / "missing-converter",
        path_prefix="worker",
        batch_id="batch-1",
    )

    consumer_cli.record_worker_manifest_start(cfg, claim)
    result = consumer_cli.run_lerobot_upload_automation(
        config=cfg,
        claim=claim,
        done_path=tmp_path / "queue/sfp/done/config_sfp_0002.yaml",
        output_root=str(tmp_path / "out"),
        run_tag=run_tag,
        collect_episode=True,
    )

    assert result["ok"] is False
    assert result["stage"] == "convert"
    assert "rosbag-to-lerobot converter entry point not found" in result["error"]
    events = read_events(manifest)
    convert_failed = [event for event in events if event["state"] == "convert_failed"]
    assert len(convert_failed) == 1
    assert convert_failed[0]["converter_path"].endswith("missing-converter")
    assert materialize(manifest)["config_sfp_0002"]["state"] == "convert_failed"


def test_recover_converted_upload_items_restores_pending_batch_after_restart(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    run_dir = tmp_path / "out/run_20260505_120000_sfp_0003"
    staged_path = tmp_path / "stage/config_sfp_0003"
    lerobot_path = tmp_path / "lerobot/items/config_sfp_0003"
    for path in (run_dir, staged_path, lerobot_path):
        path.mkdir(parents=True)
    append_event(
        manifest,
        item_id="config_sfp_0003",
        state="planned",
        batch_id="old-batch",
    )
    append_event(
        manifest,
        item_id="config_sfp_0003",
        state="worker_started",
        batch_id="old-batch",
    )
    append_event(
        manifest,
        item_id="config_sfp_0003",
        state="worker_finished",
        batch_id="old-batch",
        run_dir=str(run_dir),
    )
    append_event(
        manifest,
        item_id="config_sfp_0003",
        state="reconciled",
        batch_id="old-batch",
        run_dir=str(run_dir),
    )
    append_event(
        manifest,
        item_id="config_sfp_0003",
        state="collected_validated",
        batch_id="old-batch",
        run_dir=str(run_dir),
    )
    append_event(
        manifest,
        item_id="config_sfp_0003",
        state="staged",
        batch_id="old-batch",
        staged_path=str(staged_path),
    )
    append_event(
        manifest,
        item_id="config_sfp_0003",
        state="converted",
        batch_id="old-batch",
        staged_path=str(staged_path),
        lerobot_path=str(lerobot_path),
    )
    cfg = consumer_cli.LerobotUploadConfig(
        hf_repo_id="org/repo",
        manifest_path=manifest,
        staging_root=tmp_path / "stage",
        lerobot_root=tmp_path / "lerobot",
        converter_path=tmp_path / "converter",
        path_prefix="worker",
        batch_id="new-batch",
    )

    recovered, failures = consumer_cli.recover_converted_upload_items(cfg)

    assert failures == 0
    assert len(recovered) == 1
    assert recovered[0].item_id == "config_sfp_0003"
    assert recovered[0].run_dir == run_dir
    assert recovered[0].staged_path == staged_path
    assert recovered[0].lerobot_path == lerobot_path
    assert materialize(manifest)["config_sfp_0003"]["state"] == "converted"


def test_prepare_lerobot_upload_item_recreates_missing_manifest_start(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    run_tag = "20260505_120000_sfp_0004"
    run_dir = tmp_path / "out" / f"run_{run_tag}"
    (run_dir / "bag").mkdir(parents=True)
    (run_dir / "bag/data.mcap").write_bytes(b"mcap")
    (run_dir / "tags.json").write_text("{}")
    (run_dir / "episode").mkdir()
    (run_dir / "validation.json").write_text('{"success": true}')
    converter_path = tmp_path / "converter"
    (converter_path / "src").mkdir(parents=True)
    (converter_path / "src/main.py").write_text("# fake")

    def fake_converter(*, converter_path, input_path, output_path, config_path=None):
        output_path.mkdir(parents=True)
        (output_path / "meta.json").write_text("{}")
        return 0

    monkeypatch.setattr("aic_collector.automation.batch_runner.run_converter", fake_converter)

    claim = ClaimedConfig(
        task_type="sfp",
        sample_index=4,
        running_path=tmp_path / "queue/sfp/running/config_sfp_0004.yaml",
    )
    cfg = consumer_cli.LerobotUploadConfig(
        hf_repo_id="org/repo",
        manifest_path=manifest,
        staging_root=tmp_path / "stage",
        lerobot_root=tmp_path / "lerobot",
        converter_path=converter_path,
        path_prefix="worker",
        batch_id="batch-1",
    )

    consumer_cli.record_worker_manifest_start(cfg, claim)
    manifest.unlink()

    item, result = consumer_cli.prepare_lerobot_upload_item(
        config=cfg,
        claim=claim,
        done_path=tmp_path / "queue/sfp/done/config_sfp_0004.yaml",
        output_root=str(tmp_path / "out"),
        run_tag=run_tag,
        collect_episode=True,
    )

    assert item is not None
    assert result == {"ok": True, "stage": "converted"}
    states = [event["state"] for event in read_events(manifest)]
    assert states[:7] == [
        "planned",
        "worker_started",
        "worker_finished",
        "reconciled",
        "collected_validated",
        "staged",
        "converted",
    ]


def test_main_upload_mode_fails_before_claim_when_converter_missing(monkeypatch, tmp_path: Path, capsys) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    log_path = tmp_path / "worker.log"
    monkeypatch.setattr(
        consumer_cli.sys,
        "argv",
        [
            "aic-collector-worker",
            "--root", str(queue_root),
            "--limit", "1",
            "--log", str(log_path),
            "--hf-repo-id", "org/repo",
            "--converter-path", str(tmp_path / "missing-converter"),
        ],
    )
    monkeypatch.setattr(
        consumer_cli,
        "claim_one",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("claim_one should not run")),
    )

    rc = consumer_cli.main()

    assert rc == 2
    assert "converter entry point not found" in capsys.readouterr().err
