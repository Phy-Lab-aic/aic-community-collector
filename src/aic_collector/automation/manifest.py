from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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


def _entry_to_dict(entry: ManifestEntry) -> dict[str, Any]:
    return {
        "item_id": entry.item_id,
        "state": entry.state,
        "evidence": entry.evidence,
        "recorded_at": entry.recorded_at,
    }
