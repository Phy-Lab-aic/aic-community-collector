from __future__ import annotations

from pathlib import Path

import pytest

from aic_collector.automation.manifest import (
    InvalidTransition,
    append_event,
    cleanup_ready_items,
    latest_event,
    materialize,
    read_events,
)


def test_manifest_appends_and_materializes_latest_state(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"

    append_event(manifest, item_id="sfp-0", state="planned", batch_id="b1")
    append_event(manifest, item_id="sfp-0", state="worker_started", batch_id="b1")
    append_event(manifest, item_id="sfp-1", state="planned", batch_id="b1")

    events = read_events(manifest)
    assert [event["state"] for event in events] == [
        "planned",
        "worker_started",
        "planned",
    ]
    latest = materialize(manifest)
    assert latest["sfp-0"]["state"] == "worker_started"
    assert latest["sfp-1"]["state"] == "planned"


def test_manifest_rejects_invalid_forward_transition(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    append_event(manifest, item_id="sfp-0", state="planned", batch_id="b1")

    with pytest.raises(InvalidTransition):
        append_event(manifest, item_id="sfp-0", state="cleanup_done", batch_id="b1")


def test_manifest_idempotent_resume_keeps_single_latest_state(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    append_event(manifest, item_id="sfp-0", state="planned", batch_id="b1")
    first = append_event(manifest, item_id="sfp-0", state="worker_started", batch_id="b1")
    second = append_event(manifest, item_id="sfp-0", state="worker_started", batch_id="b1")

    assert first == second
    assert len(read_events(manifest)) == 2
    assert latest_event(manifest, "sfp-0")["state"] == "worker_started"


def test_cleanup_ready_requires_remote_verified(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    append_event(
        manifest,
        item_id="sfp-0",
        state="planned",
        batch_id="b1",
        cleanup_paths=[str(tmp_path / "unsafe")],
    )
    append_event(manifest, item_id="sfp-0", state="worker_started", batch_id="b1")
    assert cleanup_ready_items(manifest) == []

    for state in (
        "worker_finished",
        "reconciled",
        "collected_validated",
        "staged",
        "converted",
        "uploaded",
    ):
        append_event(manifest, item_id="sfp-0", state=state, batch_id="b1")
        assert cleanup_ready_items(manifest) == []

    append_event(
        manifest,
        item_id="sfp-0",
        state="remote_verified",
        batch_id="b1",
        cleanup_paths=[str(tmp_path / "safe")],
    )
    ready = cleanup_ready_items(manifest)
    assert len(ready) == 1
    assert ready[0]["item_id"] == "sfp-0"
