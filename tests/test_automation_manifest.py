from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.automation.manifest import (  # noqa: E402
    CleanupNotAllowedError,
    ManifestTransitionError,
    append_event,
    materialize_latest,
    record_cleanup_tombstone,
)


def test_append_only_events_materialize_latest_state(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"

    append_event(manifest_path, item_id="batch-001", state="planned", evidence={"count": 2})
    append_event(manifest_path, item_id="batch-001", state="worker_started", evidence={"pid": 123})

    latest = materialize_latest(manifest_path)

    assert latest["batch-001"].state == "worker_started"
    assert latest["batch-001"].evidence == {"pid": 123}
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 2


def test_invalid_forward_or_backward_transition_is_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    append_event(manifest_path, item_id="batch-001", state="planned")

    with pytest.raises(ManifestTransitionError):
        append_event(manifest_path, item_id="batch-001", state="converted")

    append_event(manifest_path, item_id="batch-001", state="worker_started")

    with pytest.raises(ManifestTransitionError):
        append_event(manifest_path, item_id="batch-001", state="planned")

    assert materialize_latest(manifest_path)["batch-001"].state == "worker_started"
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 2


def test_cleanup_gate_requires_remote_verified_and_records_tombstone(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    item_id = "batch-001"
    append_event(manifest_path, item_id=item_id, state="planned")
    append_event(manifest_path, item_id=item_id, state="worker_started")

    before = manifest_path.read_text(encoding="utf-8")
    with pytest.raises(CleanupNotAllowedError):
        record_cleanup_tombstone(
            manifest_path,
            item_id=item_id,
            deleted_paths=[tmp_path / "collected"],
        )
    assert manifest_path.read_text(encoding="utf-8") == before

    for state in (
        "worker_finished",
        "reconciled",
        "collected_validated",
        "staged",
        "converted",
        "uploaded",
        "remote_verified",
    ):
        append_event(manifest_path, item_id=item_id, state=state)

    tombstone = record_cleanup_tombstone(
        manifest_path,
        item_id=item_id,
        deleted_paths=[tmp_path / "collected", tmp_path / "staged"],
    )

    latest = materialize_latest(manifest_path)[item_id]
    assert tombstone.state == "cleanup_done"
    assert latest.state == "cleanup_done"
    assert latest.evidence["deleted_paths"] == [
        str(tmp_path / "collected"),
        str(tmp_path / "staged"),
    ]
