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


def test_prepare_lerobot_upload_batch_runs_converter_once_for_all_items(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    output_root = tmp_path / "out"
    runs: list[consumer_cli.CollectedRun] = []
    for index in (1, 2):
        run_tag = f"20260505_120000_sfp_{index:04d}"
        run_dir = output_root / f"run_{run_tag}"
        (run_dir / "bag").mkdir(parents=True)
        (run_dir / "bag/data.mcap").write_bytes(b"mcap")
        (run_dir / "tags.json").write_text("{}")
        (run_dir / "episode").mkdir()
        (run_dir / "validation.json").write_text('{"success": true}')
        claim = ClaimedConfig(
            task_type="sfp",
            sample_index=index,
            running_path=tmp_path / f"queue/sfp/running/config_sfp_{index:04d}.yaml",
        )
        runs.append(
            consumer_cli.CollectedRun(
                claim=claim,
                done_path=tmp_path / f"queue/sfp/done/config_sfp_{index:04d}.yaml",
                run_tag=run_tag,
                collect_started_at="2026-05-05T12:00:00",
                collect_duration_sec=1,
            )
        )

    converter_path = tmp_path / "converter"
    _make_fake_converter(converter_path)
    cfg = consumer_cli.LerobotUploadConfig(
        hf_repo_id="org/repo",
        manifest_path=manifest,
        staging_root=tmp_path / "stage",
        lerobot_root=tmp_path / "lerobot",
        converter_path=converter_path,
        path_prefix="worker",
        batch_id="batch-1",
    )
    calls: list[tuple[Path, Path]] = []

    def fake_converter(*, converter_path, input_path, output_path, config_path=None):
        calls.append((input_path, output_path))
        output_path.mkdir(parents=True)
        (output_path / "meta.json").write_text("{}")
        return 0

    monkeypatch.setattr("aic_collector.automation.batch_runner.run_converter", fake_converter)

    batch, result = consumer_cli.prepare_lerobot_upload_batch(
        config=cfg,
        runs=runs,
        output_root=str(output_root),
        collect_episode=True,
        batch_index=1,
    )

    assert result["ok"] is True
    assert batch is not None
    assert batch.item_ids == ["config_sfp_0001", "config_sfp_0002"]
    assert len(calls) == 1
    input_path, output_path = calls[0]
    assert sorted(path.name for path in input_path.iterdir()) == ["config_sfp_0001", "config_sfp_0002"]
    assert all((input_path / item_id / "data.mcap").exists() for item_id in batch.item_ids)
    assert output_path == tmp_path / "lerobot/upload_batches/batch-1/batch_0001"
    states_by_item = {
        item_id: [event["state"] for event in read_events(manifest) if event["item_id"] == item_id]
        for item_id in batch.item_ids
    }
    assert states_by_item["config_sfp_0001"][-1] == "converted"
    assert states_by_item["config_sfp_0002"][-1] == "converted"


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


def _build_upload_queue(queue_root: Path, count: int) -> list[Path]:
    """Create <count> sfp pending yaml files and return their paths."""
    pending = queue_root / "sfp" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for i in range(1, count + 1):
        path = pending / f"config_sfp_{i:04d}.yaml"
        path.write_text(f"# placeholder for sample {i}\n")
        files.append(path)
    return files


def _make_fake_converter(converter_path: Path) -> None:
    (converter_path / "src").mkdir(parents=True, exist_ok=True)
    (converter_path / "src/main.py").write_text("# fake converter\n")


def _patch_main_loop_phases(monkeypatch, *, call_log: list[tuple[str, str]]) -> None:
    """Patch run_one / batch prepare / upload to record phase ordering."""

    def fake_run_one(running_path, **kwargs):
        call_log.append(("collect", Path(running_path).name))
        return 0

    def fake_prepare_batch(*, config, runs, output_root, collect_episode, batch_index):
        item_ids = [run.claim.name.removesuffix(".yaml") for run in runs]
        call_log.append(("convert", ",".join(item_ids)))
        batch = consumer_cli.PreparedLerobotBatch(
            item_ids=item_ids,
            run_dirs=[Path(output_root) / f"run_{run.run_tag}" for run in runs],
            staged_paths=[config.staging_root / item_id for item_id in item_ids],
            batch_staging_path=config.staging_root / "batches" / f"batch_{batch_index:04d}",
            lerobot_path=config.lerobot_root / "upload_batches" / f"batch_{batch_index:04d}",
        )
        return batch, {"ok": True, "stage": "converted", "batch_item_id": f"batch_{batch_index:04d}"}

    def fake_upload_batch(*, config, batch=None, items=None, batch_index):
        item_ids = batch.item_ids if batch is not None else [item.item_id for item in items]
        call_log.append((
            "upload",
            f"batch_{batch_index:04d}:{','.join(item_ids)}",
        ))
        return {
            "ok": True,
            "stage": "remote_verified",
            "batch_item_id": f"batch_{batch_index:04d}",
        }

    monkeypatch.setattr(consumer_cli, "run_one", fake_run_one)
    monkeypatch.setattr(consumer_cli, "prepare_lerobot_upload_batch", fake_prepare_batch)
    monkeypatch.setattr(consumer_cli, "upload_converted_lerobot_batch", fake_upload_batch)
    monkeypatch.setattr(consumer_cli, "upload_lerobot_batch", fake_upload_batch)


def test_main_upload_mode_collects_full_batch_before_converting(monkeypatch, tmp_path: Path) -> None:
    """Phase 1 must finish all collects before any convert; phase 3 uploads once per batch."""
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    _build_upload_queue(queue_root, count=5)
    converter_path = tmp_path / "converter"
    _make_fake_converter(converter_path)
    output_root = tmp_path / "out"
    output_root.mkdir()

    call_log: list[tuple[str, str]] = []
    _patch_main_loop_phases(monkeypatch, call_log=call_log)
    monkeypatch.setattr(
        consumer_cli,
        "_default_worker_batch_id",
        lambda now=None: "batch-test-1",
    )

    monkeypatch.setattr(
        consumer_cli.sys,
        "argv",
        [
            "aic-collector-worker",
            "--root", str(queue_root),
            "--task", "sfp",
            "--limit", "5",
            "--state-file", str(tmp_path / "state.json"),
            "--hf-repo-id", "org/repo",
            "--converter-path", str(converter_path),
            "--output-root", str(output_root),
            "--staging-root", str(tmp_path / "stage"),
            "--lerobot-root", str(tmp_path / "lerobot"),
            "--automation-manifest", str(tmp_path / "manifest.jsonl"),
            "--upload-batch-size", "5",
            "--no-cleanup-after-upload",
        ],
    )

    rc = consumer_cli.main()
    assert rc == 0

    phases = [phase for phase, _ in call_log]
    # All 5 collects must happen before one batch conversion and one upload.
    assert phases == ["collect"] * 5 + ["convert", "upload"]

    upload_payload = next(item for phase, item in call_log if phase == "upload")
    item_ids = upload_payload.split(":", maxsplit=1)[1].split(",")
    assert item_ids == [f"config_sfp_{i:04d}" for i in range(1, 6)]


def test_main_upload_mode_partial_last_batch_still_flushes(monkeypatch, tmp_path: Path) -> None:
    """Queue with fewer items than batch_size still completes all 3 phases on the partial batch."""
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    _build_upload_queue(queue_root, count=3)
    converter_path = tmp_path / "converter"
    _make_fake_converter(converter_path)
    output_root = tmp_path / "out"
    output_root.mkdir()

    call_log: list[tuple[str, str]] = []
    _patch_main_loop_phases(monkeypatch, call_log=call_log)
    monkeypatch.setattr(
        consumer_cli,
        "_default_worker_batch_id",
        lambda now=None: "batch-test-partial",
    )

    monkeypatch.setattr(
        consumer_cli.sys,
        "argv",
        [
            "aic-collector-worker",
            "--root", str(queue_root),
            "--task", "sfp",
            "--state-file", str(tmp_path / "state.json"),
            "--hf-repo-id", "org/repo",
            "--converter-path", str(converter_path),
            "--output-root", str(output_root),
            "--staging-root", str(tmp_path / "stage"),
            "--lerobot-root", str(tmp_path / "lerobot"),
            "--automation-manifest", str(tmp_path / "manifest.jsonl"),
            "--upload-batch-size", "5",
            "--no-cleanup-after-upload",
        ],
    )

    rc = consumer_cli.main()
    assert rc == 0

    phases = [phase for phase, _ in call_log]
    assert phases == ["collect"] * 3 + ["convert", "upload"]


def test_main_upload_mode_recovered_items_flush_on_first_batch(monkeypatch, tmp_path: Path) -> None:
    """Items recovered from an earlier worker run join the first batch upload."""
    from aic_collector.automation.manifest import append_event

    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    _build_upload_queue(queue_root, count=2)
    converter_path = tmp_path / "converter"
    _make_fake_converter(converter_path)
    output_root = tmp_path / "out"
    output_root.mkdir()
    manifest_path = tmp_path / "manifest.jsonl"

    # Simulate one converted-but-not-uploaded survivor from a previous run.
    survivor_id = "config_sfp_9999"
    survivor_run = tmp_path / "out" / "run_prev"
    survivor_stage = tmp_path / "stage" / survivor_id
    survivor_lerobot = tmp_path / "lerobot" / "items" / survivor_id
    for path in (survivor_run, survivor_stage, survivor_lerobot):
        path.mkdir(parents=True)
    append_event(manifest_path, item_id=survivor_id, state="planned", batch_id="prev-batch")
    append_event(manifest_path, item_id=survivor_id, state="worker_started", batch_id="prev-batch")
    append_event(
        manifest_path, item_id=survivor_id, state="worker_finished", batch_id="prev-batch",
        run_dir=str(survivor_run),
    )
    append_event(
        manifest_path, item_id=survivor_id, state="reconciled", batch_id="prev-batch",
        run_dir=str(survivor_run),
    )
    append_event(
        manifest_path, item_id=survivor_id, state="collected_validated", batch_id="prev-batch",
        run_dir=str(survivor_run),
    )
    append_event(
        manifest_path, item_id=survivor_id, state="staged", batch_id="prev-batch",
        staged_path=str(survivor_stage),
    )
    append_event(
        manifest_path, item_id=survivor_id, state="converted", batch_id="prev-batch",
        run_dir=str(survivor_run), staged_path=str(survivor_stage),
        lerobot_path=str(survivor_lerobot),
    )

    call_log: list[tuple[str, str]] = []
    _patch_main_loop_phases(monkeypatch, call_log=call_log)
    monkeypatch.setattr(
        consumer_cli,
        "_default_worker_batch_id",
        lambda now=None: "batch-test-recovery",
    )

    monkeypatch.setattr(
        consumer_cli.sys,
        "argv",
        [
            "aic-collector-worker",
            "--root", str(queue_root),
            "--task", "sfp",
            "--state-file", str(tmp_path / "state.json"),
            "--hf-repo-id", "org/repo",
            "--converter-path", str(converter_path),
            "--output-root", str(output_root),
            "--staging-root", str(tmp_path / "stage"),
            "--lerobot-root", str(tmp_path / "lerobot"),
            "--automation-manifest", str(manifest_path),
            "--upload-batch-size", "2",
            "--no-cleanup-after-upload",
        ],
    )

    rc = consumer_cli.main()
    assert rc == 0

    upload_payloads = [item for phase, item in call_log if phase == "upload"]
    assert len(upload_payloads) == 2
    uploaded_ids = ",".join(upload_payloads)
    assert survivor_id in uploaded_ids
    assert "config_sfp_0001" in uploaded_ids
    assert "config_sfp_0002" in uploaded_ids


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
