"""Tests for multi-PC round helpers (aggregate / verify-repo / retry-uploads)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.automation.round_helpers import (  # noqa: E402
    aggregate_manifests,
    retry_failed_uploads,
    verify_repo_against_ledger,
)


def _seed_manifest(path: Path, events: list[dict]) -> Path:
    """Write raw JSONL events directly. Bypasses transition validation so tests
    can construct any state combination they need."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for i, ev in enumerate(events):
            payload = dict(ev)
            payload.setdefault("schema_version", 1)
            payload.setdefault("timestamp", f"2026-05-07T00:00:{i:02d}+00:00")
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    return path


def test_aggregate_manifests_combines_states_and_failures(tmp_path: Path) -> None:
    pc1 = _seed_manifest(tmp_path / "pc1.jsonl", [
        {"item_id": "config_sfp_000000", "state": "uploaded", "batch_id": "b1"},
        {"item_id": "config_sfp_000000", "state": "remote_verified", "batch_id": "b1"},
        {"item_id": "config_sfp_000001", "state": "planned", "batch_id": "b1"},
        {"item_id": "config_sfp_000001", "state": "worker_started", "batch_id": "b1"},
        {"item_id": "config_sfp_000001", "state": "worker_failed", "batch_id": "b1"},
    ])
    pc2 = _seed_manifest(tmp_path / "pc2.jsonl", [
        {"item_id": "config_sfp_100000", "state": "uploaded", "batch_id": "b2"},
        {"item_id": "config_sfp_100000", "state": "upload_failed", "batch_id": "b2"},
    ])

    rollup = aggregate_manifests([pc1, pc2])

    assert set(rollup["items"]) == {
        "config_sfp_000000", "config_sfp_000001", "config_sfp_100000"
    }
    assert rollup["state_counts"]["remote_verified"] == 1
    assert rollup["state_counts"]["worker_failed"] == 1
    assert rollup["state_counts"]["upload_failed"] == 1
    failure_ids = {e["item_id"] for e in rollup["failures"]}
    assert failure_ids == {"config_sfp_000001", "config_sfp_100000"}


def test_aggregate_manifests_deduplicates_same_item_across_pcs(tmp_path: Path) -> None:
    pc1 = _seed_manifest(tmp_path / "pc1.jsonl", [
        {"item_id": "config_sfp_000000", "state": "planned", "batch_id": "b1"},
        {"item_id": "config_sfp_000000", "state": "uploaded", "batch_id": "b1"},
    ])
    pc2 = _seed_manifest(tmp_path / "pc2.jsonl", [
        {"item_id": "config_sfp_000000", "state": "planned", "batch_id": "b2"},
    ])
    # PC1 has the later timestamp progression (uploaded). It should win.
    rollup = aggregate_manifests([pc2, pc1])
    assert rollup["items"]["config_sfp_000000"]["state"] == "uploaded"


def _seed_ledger(path: Path, entries: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"entries": entries}, sort_keys=False), encoding="utf-8")


def test_verify_repo_against_ledger_flags_missing_and_extra(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.yaml"
    _seed_ledger(ledger, [
        {"member_id": "M0", "task_type": "sfp", "start_index": 0, "count": 3},
        {"member_id": "M1", "task_type": "sc", "start_index": 100000, "count": 2},
    ])

    api = MagicMock()
    api.list_repo_files.return_value = [
        # SFP 0 and 1 present; index 2 missing.
        "round/batchA/batch_0001/config_sfp_000000/episode/data.parquet",
        "round/batchA/batch_0001/config_sfp_000001/meta.json",
        # An SFP file outside the ledger range -> "extra".
        "round/batchA/batch_0001/config_sfp_999999/something",
        # SC indices both present.
        "round/batchA/batch_0001/config_sc_100000/x",
        "round/batchA/batch_0001/config_sc_100001/y",
    ]

    report = verify_repo_against_ledger(api=api, repo_id="org/aic", ledger_path=ledger)
    assert report["ok"] is False
    assert report["tasks"]["sfp"]["missing"] == [2]
    assert report["tasks"]["sfp"]["extra"] == [999999]
    assert report["tasks"]["sc"]["missing"] == []
    assert report["tasks"]["sc"]["extra"] == []


def test_verify_repo_passes_when_inventory_matches(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.yaml"
    _seed_ledger(ledger, [
        {"member_id": "M0", "task_type": "sfp", "start_index": 0, "count": 2},
    ])
    api = MagicMock()
    api.list_repo_files.return_value = [
        "x/y/config_sfp_000000/a", "x/y/config_sfp_000001/b",
    ]
    report = verify_repo_against_ledger(api=api, repo_id="org/aic", ledger_path=ledger)
    assert report["ok"] is True
    assert report["tasks"]["sfp"]["present"] == 2


def test_retry_failed_uploads_skips_when_folder_missing(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _seed_manifest(manifest, [
        {"item_id": "config_sfp_000001", "state": "uploaded", "batch_id": "b1",
         "batch_folder": str(tmp_path / "nope")},
        {"item_id": "config_sfp_000001", "state": "upload_failed", "batch_id": "b1",
         "batch_folder": str(tmp_path / "nope")},
    ])

    api = MagicMock()
    report = retry_failed_uploads(
        manifest_path=manifest, repo_id="org/aic", api=api, max_attempts=2,
    )
    assert len(report) == 1
    r = report[0]
    assert r["ok"] is False
    assert r["missing_folder"] is True
    api.upload_folder.assert_not_called()


def test_retry_failed_uploads_resubmits_when_folder_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "stage"
    folder.mkdir()
    (folder / "x.txt").write_text("hi")

    manifest = tmp_path / "manifest.jsonl"
    _seed_manifest(manifest, [
        {"item_id": "config_sfp_000002", "state": "uploaded", "batch_id": "b1",
         "batch_folder": str(folder)},
        {"item_id": "config_sfp_000002", "state": "upload_failed", "batch_id": "b1",
         "batch_folder": str(folder)},
    ])

    # Stub record_upload_and_verify to avoid touching the real HF API.
    calls: list[dict] = []

    def fake_record_upload_and_verify(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "missing": []}

    monkeypatch.setattr(
        "aic_collector.automation.batch_runner.record_upload_and_verify",
        fake_record_upload_and_verify,
    )

    api = MagicMock()
    report = retry_failed_uploads(
        manifest_path=manifest, repo_id="org/aic", api=api,
        max_attempts=3, backoff_seconds=0.0,
    )
    assert len(report) == 1
    assert report[0]["ok"] is True
    assert report[0]["attempts"] == 1
    assert len(calls) == 1
    assert calls[0]["repo_id"] == "org/aic"
    assert calls[0]["local_folder"] == folder


def test_retry_failed_uploads_retries_until_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "stage"
    folder.mkdir()
    (folder / "x.txt").write_text("hi")
    manifest = tmp_path / "manifest.jsonl"
    _seed_manifest(manifest, [
        {"item_id": "i", "state": "uploaded", "batch_id": "b",
         "batch_folder": str(folder)},
        {"item_id": "i", "state": "upload_failed", "batch_id": "b",
         "batch_folder": str(folder)},
    ])

    attempts: list[int] = []

    def fake(**kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("412 conflict")
        return {"ok": True}

    monkeypatch.setattr(
        "aic_collector.automation.batch_runner.record_upload_and_verify", fake,
    )

    report = retry_failed_uploads(
        manifest_path=manifest, repo_id="r", api=MagicMock(),
        max_attempts=3, backoff_seconds=0.0,
    )
    assert report[0]["ok"] is True
    assert report[0]["attempts"] == 2
    assert len(attempts) == 2
