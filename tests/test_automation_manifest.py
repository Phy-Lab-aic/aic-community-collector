from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.automation.manifest import append_event, materialize_latest  # noqa: E402


def test_append_only_events_materialize_latest_state(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"

    append_event(manifest_path, item_id="batch-001", state="planned", evidence={"count": 2})
    append_event(manifest_path, item_id="batch-001", state="worker_started", evidence={"pid": 123})

    latest = materialize_latest(manifest_path)

    assert latest["batch-001"].state == "worker_started"
    assert latest["batch-001"].evidence == {"pid": 123}
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 2
