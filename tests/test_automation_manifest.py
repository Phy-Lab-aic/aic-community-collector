from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.automation.manifest import (  # noqa: E402
    InvalidTransition,
    append_event,
    materialize,
    read_events,
)


def test_append_only_events_materialize_latest_state(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"

    append_event(manifest_path, item_id="batch-001", state="planned", count=2)
    append_event(manifest_path, item_id="batch-001", state="worker_started", pid=123)

    events = read_events(manifest_path)
    latest = materialize(manifest_path)

    assert [event["state"] for event in events] == ["planned", "worker_started"]
    assert latest["batch-001"]["state"] == "worker_started"
    assert latest["batch-001"]["pid"] == 123
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 2


def test_invalid_forward_or_backward_transition_is_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    append_event(manifest_path, item_id="batch-001", state="planned")

    with pytest.raises(InvalidTransition):
        append_event(manifest_path, item_id="batch-001", state="converted")

    append_event(manifest_path, item_id="batch-001", state="worker_started")

    with pytest.raises(InvalidTransition):
        append_event(manifest_path, item_id="batch-001", state="planned")

    assert materialize(manifest_path)["batch-001"]["state"] == "worker_started"
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 2


def test_cleanup_gate_requires_remote_verified_and_records_tombstone(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    item_id = "batch-001"
    append_event(manifest_path, item_id=item_id, state="planned")
    append_event(manifest_path, item_id=item_id, state="worker_started")

    before = manifest_path.read_text(encoding="utf-8")
    with pytest.raises(InvalidTransition):
        append_event(
            manifest_path,
            item_id=item_id,
            state="cleanup_done",
            deleted_paths=[str(tmp_path / "collected")],
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

    tombstone = append_event(
        manifest_path,
        item_id=item_id,
        state="cleanup_done",
        deleted_paths=[str(tmp_path / "collected"), str(tmp_path / "staged")],
    )

    latest = materialize(manifest_path)[item_id]
    assert tombstone["state"] == "cleanup_done"
    assert latest["state"] == "cleanup_done"
    assert latest["deleted_paths"] == [
        str(tmp_path / "collected"),
        str(tmp_path / "staged"),
    ]
