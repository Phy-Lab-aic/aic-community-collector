"""Append-only automation manifest.

The manifest is the safety ledger for batch automation.  Queue state can prove a
config finished locally, but only this manifest can make cleanup eligible after
remote Hugging Face verification has been recorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Sequence
from typing import Any

FORWARD_STATES: tuple[str, ...] = (
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
FAILURE_STATES: frozenset[str] = frozenset(
    {
        "worker_failed",
        "reconcile_failed",
        "validation_failed",
        "stage_failed",
        "convert_failed",
        "upload_failed",
        "remote_verify_failed",
        "cleanup_failed",
    }
)
_INITIAL_STATES: frozenset[str] = frozenset({"planned", "uploaded", "remote_verified"})
_RECOVERY_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("stage_failed", "staged"),
        ("convert_failed", "converted"),
        ("upload_failed", "uploaded"),
        ("remote_verify_failed", "uploaded"),
        ("cleanup_failed", "cleanup_done"),
    }
)


class ManifestTransitionError(ValueError):
    """Raised when a manifest item attempts an unsafe state transition."""


class InvalidTransition(ManifestTransitionError):
    """Backward-compatible alias for invalid manifest transitions."""


class CleanupNotAllowedError(ValueError):
    """Raised when cleanup is attempted before remote verification."""


@dataclass(frozen=True)
class ManifestEntry:
    item_id: str
    state: str
    evidence: dict[str, Any]
    recorded_at: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_events(manifest_path: Path) -> list[dict[str, Any]]:
    """Read JSONL manifest events, returning an empty list for a new manifest."""
    if not manifest_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive corruption guard
            raise ValueError(f"Invalid manifest JSON at {manifest_path}:{line_no}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"Invalid manifest event at {manifest_path}:{line_no}")
        events.append(event)
    return events


def materialize(manifest_path: Path) -> dict[str, dict[str, Any]]:
    """Return latest event by item id."""
    latest: dict[str, dict[str, Any]] = {}
    for event in read_events(manifest_path):
        item_id = str(event.get("item_id", ""))
        if not item_id:
            continue
        latest[item_id] = event
    return latest


def latest_event(manifest_path: Path, item_id: str) -> dict[str, Any] | None:
    return materialize(manifest_path).get(item_id)


def materialize_latest(manifest_path: Path) -> dict[str, ManifestEntry]:
    """Return latest manifest entries in the dataclass shape used by tests/UI."""
    latest: dict[str, ManifestEntry] = {}
    for item_id, event in materialize(manifest_path).items():
        evidence = event.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {
                key: value
                for key, value in event.items()
                if key not in {"schema_version", "timestamp", "item_id", "state", "batch_id"}
            }
        latest[item_id] = ManifestEntry(
            item_id=item_id,
            state=str(event.get("state", "")),
            evidence=dict(evidence),
            recorded_at=str(event.get("timestamp", "")),
        )
    return latest


def _validate_transition(previous_state: str | None, next_state: str) -> None:
    if next_state not in FORWARD_STATES and next_state not in FAILURE_STATES:
        raise InvalidTransition(f"Unknown manifest state: {next_state!r}")
    if previous_state is None:
        if next_state not in _INITIAL_STATES:
            raise InvalidTransition(f"Initial state cannot be {next_state!r}")
        return
    if previous_state == next_state:
        return
    if next_state in FAILURE_STATES:
        return
    if previous_state in FAILURE_STATES:
        if (previous_state, next_state) in _RECOVERY_TRANSITIONS:
            return
        raise InvalidTransition(f"Cannot transition from failure state {previous_state!r} to {next_state!r}")
    try:
        previous_index = FORWARD_STATES.index(previous_state)
        next_index = FORWARD_STATES.index(next_state)
    except ValueError as exc:
        raise InvalidTransition(f"Invalid transition {previous_state!r} -> {next_state!r}") from exc
    if next_index != previous_index + 1:
        # cleanup_done may be appended directly by cleanup after remote verification;
        # cleanup_eligible is useful evidence but not mandatory for safe deletion.
        if previous_state == "remote_verified" and next_state == "cleanup_done":
            return
        raise InvalidTransition(f"Invalid transition {previous_state!r} -> {next_state!r}")


def append_event(
    manifest_path: Path,
    *,
    item_id: str,
    state: str,
    batch_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    """Append a validated event.

    Re-appending the current state is idempotent and returns the existing latest
    event without writing a duplicate line.
    """
    latest = latest_event(manifest_path, item_id)
    previous_state = str(latest["state"]) if latest else None
    _validate_transition(previous_state, state)
    if latest is not None and latest.get("state") == state:
        return latest

    event: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": _now_iso(),
        "item_id": item_id,
        "state": state,
    }
    if batch_id is not None:
        event["batch_id"] = batch_id
    event.update(payload)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
    return event


def cleanup_ready_items(manifest_path: Path) -> list[dict[str, Any]]:
    """Return latest manifest entries that may be deleted locally."""
    return [event for event in materialize(manifest_path).values() if event.get("state") == "remote_verified"]


def record_cleanup_tombstone(
    manifest_path: Path,
    *,
    item_id: str,
    deleted_paths: Sequence[Path | str],
) -> ManifestEntry:
    """Append a cleanup tombstone only after remote verification evidence exists."""
    latest = latest_event(manifest_path, item_id)
    if latest is None or latest.get("state") != "remote_verified":
        raise CleanupNotAllowedError(f"{item_id} is not remote_verified")
    append_event(
        manifest_path,
        item_id=item_id,
        state="cleanup_done",
        batch_id=latest.get("batch_id"),
        evidence={"deleted_paths": [str(path) for path in deleted_paths]},
    )
    return materialize_latest(manifest_path)[item_id]
