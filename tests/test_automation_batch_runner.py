from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

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

    staged = stage_run_artifacts(
        run_dir=run_dir,
        staging_root=tmp_path / "stage",
        item_id="sfp-0",
    )

    assert source_mcap.exists()
    assert (staged / "bag" / "data.mcap").read_bytes() == b"mcap"


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
