from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


STATE_ORDER = (
    "planned",
    "worker_started",
    "worker_finished",
    "reconciled",
    "collected_validated",
    "staged",
    "converted",
    "uploaded",
    "remote_verified",
    "cleanup_eligible",
    "cleanup_done",
)


class ManifestTransitionError(ValueError):
    """Raised when a manifest event would violate the batch state machine."""


class CleanupNotAllowedError(ValueError):
    """Raised when cleanup is requested before remote verification exists."""


@dataclass(frozen=True)
class ManifestEntry:
    item_id: str
    state: str
    evidence: dict[str, Any]
    recorded_at: str


def append_event(
    manifest_path: Path,
    *,
    item_id: str,
    state: str,
    evidence: dict[str, Any] | None = None,
) -> ManifestEntry:
    _validate_transition(manifest_path, item_id=item_id, state=state)
    entry = ManifestEntry(
        item_id=item_id,
        state=state,
        evidence=dict(evidence or {}),
        recorded_at=datetime.now(UTC).isoformat(),
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_entry_to_dict(entry), sort_keys=True) + "\n")
    return entry


def materialize_latest(manifest_path: Path) -> dict[str, ManifestEntry]:
    latest: dict[str, ManifestEntry] = {}
    if not manifest_path.exists():
        return latest
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        entry = ManifestEntry(
            item_id=raw["item_id"],
            state=raw["state"],
            evidence=dict(raw.get("evidence") or {}),
            recorded_at=raw["recorded_at"],
        )
        latest[entry.item_id] = entry
    return latest


def record_cleanup_tombstone(
    manifest_path: Path,
    *,
    item_id: str,
    deleted_paths: list[Path],
) -> ManifestEntry:
    latest = materialize_latest(manifest_path).get(item_id)
    if latest is None or latest.state != "remote_verified":
        raise CleanupNotAllowedError(
            f"cleanup requires latest state remote_verified for {item_id}"
        )

    append_event(manifest_path, item_id=item_id, state="cleanup_eligible")
    return append_event(
        manifest_path,
        item_id=item_id,
        state="cleanup_done",
        evidence={"deleted_paths": [str(path) for path in deleted_paths]},
    )


def _entry_to_dict(entry: ManifestEntry) -> dict[str, Any]:
    return {
        "item_id": entry.item_id,
        "state": entry.state,
        "evidence": entry.evidence,
        "recorded_at": entry.recorded_at,
    }


def _validate_transition(manifest_path: Path, *, item_id: str, state: str) -> None:
    if state not in STATE_ORDER:
        raise ManifestTransitionError(f"unknown manifest state: {state}")

    latest = materialize_latest(manifest_path).get(item_id)
    if latest is None:
        if state != STATE_ORDER[0]:
            raise ManifestTransitionError("first manifest state must be planned")
        return

    current_index = STATE_ORDER.index(latest.state)
    next_index = STATE_ORDER.index(state)
    if next_index not in {current_index, current_index + 1}:
        raise ManifestTransitionError(
            f"invalid transition for {item_id}: {latest.state} -> {state}"
        )
