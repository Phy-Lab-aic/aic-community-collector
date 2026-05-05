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

    assert latest["batch-001"].state == "worker_started"
    assert latest["batch-001"].evidence == {"pid": 123}
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 2

from aic_collector.automation.manifest import ManifestTransitionError  # noqa: E402


def test_invalid_forward_or_backward_transition_is_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    append_event(manifest_path, item_id="batch-001", state="planned")

    try:
        append_event(manifest_path, item_id="batch-001", state="converted")
    except ManifestTransitionError:
        pass
    else:  # pragma: no cover - failure path assertion
        raise AssertionError("skipping required states must be rejected")

    append_event(manifest_path, item_id="batch-001", state="worker_started")

    try:
        append_event(manifest_path, item_id="batch-001", state="planned")
    except ManifestTransitionError:
        pass
    else:  # pragma: no cover - failure path assertion
        raise AssertionError("backward transitions must be rejected")

    assert materialize_latest(manifest_path)["batch-001"].state == "worker_started"
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 2
