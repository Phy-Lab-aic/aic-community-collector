from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aic_collector.automation import batch_runner
from aic_collector.automation.batch_runner import (
    build_worker_command,
    cleanup_verified_paths,
    record_upload_and_verify,
    resume_uploaded_remote_verification,
    stage_run_artifacts,
    verify_remote_upload,
)
from aic_collector.automation.manifest import append_event, latest_event, materialize


def test_worker_command_uses_private_roots_and_state_file(tmp_path: Path) -> None:
    cmd = build_worker_command(
        queue_root=tmp_path / "queue" / "batch-1",
        output_root=tmp_path / "out" / "batch-1",
        batch_size=3,
        state_file=tmp_path / "state.json",
        log_file=tmp_path / "worker.log",
        policy="cheatcode",
        task="all",
        timeout=60,
        headless=True,
    )

    assert cmd[:3] == ["uv", "run", "aic-collector-worker"]
    assert cmd[cmd.index("--root") + 1].endswith("queue/batch-1")
    assert cmd[cmd.index("--output-root") + 1].endswith("out/batch-1")
    assert cmd[cmd.index("--limit") + 1] == "3"
    assert cmd[cmd.index("--state-file") + 1].endswith("state.json")
    assert "--collect-episode" in cmd
    assert cmd[cmd.index("--collect-episode") + 1] == "true"
    assert "--headless" in cmd


def test_stage_run_artifacts_is_non_destructive(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_20260505_sfp_0000"
    bag_dir = run_dir / "bag"
    bag_dir.mkdir(parents=True)
    source_mcap = bag_dir / "data.mcap"
    source_mcap.write_bytes(b"mcap")
    (run_dir / "validation.json").write_text('{"success": true}')
    (run_dir / "episode").mkdir()
    (run_dir / "episode" / "metadata.json").write_text("{}")

    staged = stage_run_artifacts(
        run_dir=run_dir,
        staging_root=tmp_path / "stage",
        item_id="sfp-0",
    )

    converter_run = staged / "sfp-0"
    assert source_mcap.exists()
    assert (converter_run / "data.mcap").read_bytes() == b"mcap"
    assert (converter_run / "validation.json").exists()
    assert (converter_run / "episode" / "metadata.json").exists()


def test_upload_event_is_recorded_before_remote_verification(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "manifest.jsonl"
    dataset = tmp_path / "lerobot"
    dataset.mkdir()
    (dataset / "meta.json").write_text("{}")
    calls: list[str] = []

    class FakeApi:
        def upload_folder(self, **kwargs):
            calls.append("upload")
            return "https://hf.co/datasets/org/repo/commit/abc123"

    monkeypatch.setattr(batch_runner, "HfApi", lambda: FakeApi())
    monkeypatch.setattr(
        batch_runner,
        "verify_remote_upload",
        lambda **kwargs: {"ok": True, "revision": "abc123", "files": ["meta.json"]},
    )

    record_upload_and_verify(
        manifest_path=manifest,
        item_id="sfp-0",
        batch_id="b1",
        local_folder=dataset,
        repo_id="org/repo",
        path_in_repo="batch/sfp-0",
    )

    states = [event["state"] for event in batch_runner.read_events(manifest)]
    assert states == ["uploaded", "remote_verified"]
    uploaded = batch_runner.read_events(manifest)[0]
    assert uploaded["upload"]["commit_url"].endswith("abc123")
    assert calls == ["upload"]


def test_resume_uploaded_verifies_without_reupload(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "manifest.jsonl"
    append_event(
        manifest,
        item_id="sfp-0",
        state="uploaded",
        batch_id="b1",
        upload={"repo_id": "org/repo", "revision": "abc123", "path_in_repo": "batch/sfp-0"},
    )

    monkeypatch.setattr(
        batch_runner,
        "verify_remote_upload",
        lambda **kwargs: {"ok": True, "revision": "abc123", "files": ["meta.json"]},
    )

    verified = resume_uploaded_remote_verification(manifest)

    assert verified == ["sfp-0"]
    assert latest_event(manifest, "sfp-0")["state"] == "remote_verified"


def test_verify_remote_upload_requires_expected_files() -> None:
    class FakeApi:
        def list_repo_files(self, **kwargs):
            return ["data/meta.json", "data/data.parquet"]

    result = verify_remote_upload(
        api=FakeApi(),
        repo_id="org/repo",
        revision="abc123",
        expected_paths=["data/meta.json"],
    )
    assert result["ok"] is True

    missing = verify_remote_upload(
        api=FakeApi(),
        repo_id="org/repo",
        revision="abc123",
        expected_paths=["data/missing.json"],
    )
    assert missing["ok"] is False
    assert missing["missing"] == ["data/missing.json"]


def test_cleanup_deletes_only_remote_verified_manifest_paths(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    unsafe = tmp_path / "unsafe"
    safe = tmp_path / "safe"
    unsafe.mkdir()
    safe.mkdir()
    (unsafe / "data.txt").write_text("keep")
    (safe / "data.txt").write_text("delete")

    append_event(manifest, item_id="unsafe", state="planned", cleanup_paths=[str(unsafe)])
    append_event(manifest, item_id="safe", state="remote_verified", cleanup_paths=[str(safe)])

    deleted = cleanup_verified_paths(manifest)

    assert deleted == [str(safe)]
    assert unsafe.exists()
    assert not safe.exists()
    assert materialize(manifest)["safe"]["state"] == "cleanup_done"


def test_cleanup_never_deletes_manifest_or_containing_directory(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    manifest = ledger_root / "worker_lerobot_upload_manifest.jsonl"
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "data.txt").write_text("delete")

    append_event(
        manifest,
        item_id="batch",
        state="remote_verified",
        cleanup_paths=[str(safe), str(manifest), str(ledger_root)],
    )

    deleted = cleanup_verified_paths(manifest)

    latest = materialize(manifest)["batch"]
    assert deleted == [str(safe)]
    assert not safe.exists()
    assert manifest.exists()
    assert ledger_root.exists()
    assert latest["state"] == "cleanup_done"
    assert latest["deleted_paths"] == [str(safe)]
    assert latest["skipped_paths"] == [str(manifest), str(ledger_root)]


def test_reconcile_queue_results_records_done_and_failed(tmp_path: Path) -> None:
    from aic_collector.automation.batch_runner import reconcile_queue_results
    from aic_collector.job_queue import QueueState, queue_dir

    manifest = tmp_path / "manifest.jsonl"
    queue_root = tmp_path / "queue"
    done = queue_dir(queue_root, "sfp", QueueState.DONE)
    failed = queue_dir(queue_root, "sfp", QueueState.FAILED)
    done.mkdir(parents=True)
    failed.mkdir(parents=True)
    done_cfg = done / "config_sfp_000000.yaml"
    failed_cfg = failed / "config_sfp_000001.yaml"
    done_cfg.write_text("{}")
    failed_cfg.write_text("{}")
    append_event(manifest, item_id="config_sfp_000000", state="planned", batch_id="b1")
    append_event(manifest, item_id="config_sfp_000000", state="worker_started", batch_id="b1")
    append_event(manifest, item_id="config_sfp_000001", state="planned", batch_id="b1")
    append_event(manifest, item_id="config_sfp_000001", state="worker_started", batch_id="b1")

    result = reconcile_queue_results(
        manifest_path=manifest,
        batch_id="b1",
        queue_root=queue_root,
        expected_configs=[queue_root / "sfp/pending/config_sfp_000000.yaml", queue_root / "sfp/pending/config_sfp_000001.yaml"],
    )

    assert result == {"config_sfp_000000": "reconciled", "config_sfp_000001": "worker_failed"}
    assert materialize(manifest)["config_sfp_000000"]["state"] == "reconciled"
    assert materialize(manifest)["config_sfp_000001"]["state"] == "worker_failed"


def test_validate_run_artifacts_requires_mcap_tags_and_episode(tmp_path: Path) -> None:
    from aic_collector.automation.batch_runner import validate_run_artifacts

    run_dir = tmp_path / "run_1"
    (run_dir / "bag").mkdir(parents=True)
    (run_dir / "bag/data.mcap").write_bytes(b"mcap")
    (run_dir / "tags.json").write_text("{}")
    (run_dir / "episode").mkdir()
    (run_dir / "validation.json").write_text('{"success": true}')

    assert validate_run_artifacts(run_dir)["ok"] is True
    (run_dir / "bag/data.mcap").unlink()
    assert validate_run_artifacts(run_dir)["ok"] is False


def test_validate_run_artifacts_accepts_prefect_validation_summary(tmp_path: Path) -> None:
    from aic_collector.automation.batch_runner import validate_run_artifacts

    run_dir = tmp_path / "run_1"
    (run_dir / "bag").mkdir(parents=True)
    (run_dir / "bag/data.mcap").write_bytes(b"mcap")
    (run_dir / "tags.json").write_text("{}")
    (run_dir / "episode").mkdir()
    (run_dir / "validation.json").write_text(
        '{"checks": [{"name": "a", "passed": true}], "passed_count": 17, "total_count": 17}'
    )

    assert validate_run_artifacts(run_dir)["ok"] is True


def test_run_converter_missing_entrypoint_has_actionable_message(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="git submodule update"):
        batch_runner.run_converter(
            converter_path=tmp_path / "missing-converter",
            input_path=tmp_path / "stage",
            output_path=tmp_path / "lerobot",
        )


def test_run_converter_sets_converter_pythonpath(monkeypatch, tmp_path: Path) -> None:
    converter = tmp_path / "converter"
    (converter / "src").mkdir(parents=True)
    (converter / "src/main.py").write_text("# fake")
    (converter / "src/config.json").write_text('{"task": "aic_task", "repo_id": "org/repo"}')
    captured = {}

    def fake_run(cmd, *, env, check):
        captured["cmd"] = cmd
        captured["env"] = env
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(batch_runner.subprocess, "run", fake_run)

    rc = batch_runner.run_converter(
        converter_path=converter,
        input_path=tmp_path / "stage",
        output_path=tmp_path / "out",
    )

    assert rc == 0
    assert captured["cmd"][3].endswith("main.py")
    generated_config = Path(captured["cmd"][4])
    assert generated_config.name == "_local_converter_config.json"
    assert not generated_config.exists()
    pythonpath = captured["env"]["PYTHONPATH"].split(batch_runner.os.pathsep)
    assert str(converter / "src") in pythonpath
    assert str(converter / "lerobot" / "src") in pythonpath
    assert str(converter / "docker" / "torch-stub") in pythonpath


def test_run_converter_passes_explicit_config_as_positional_arg(monkeypatch, tmp_path: Path) -> None:
    converter = tmp_path / "converter"
    (converter / "src").mkdir(parents=True)
    (converter / "src/main.py").write_text("# fake")
    explicit_config = tmp_path / "config.json"
    explicit_config.write_text('{"task": "x"}')
    captured = {}

    def fake_run(cmd, *, env, check):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(batch_runner.subprocess, "run", fake_run)

    assert batch_runner.run_converter(
        converter_path=converter,
        input_path=tmp_path / "stage",
        output_path=tmp_path / "out",
        config_path=explicit_config,
    ) == 0
    assert captured["cmd"][-1] == str(explicit_config)
    assert "--config" not in captured["cmd"]
