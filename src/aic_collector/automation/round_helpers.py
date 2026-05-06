"""Round-coordination helpers for multi-PC HF uploads.

Three operations that each PC's worker doesn't do on its own:

- `aggregate_manifests`: roll several PCs' manifest.jsonl files into one
  per-state count + per-item-id latest-state map. The coordinator runs this
  to see overall round progress.
- `verify_repo_against_ledger`: list files in the HF dataset repo and
  cross-check sample_index coverage against ledger claims. Catches uploads
  that were marked verified locally but never landed remotely.
- `retry_failed_uploads`: re-issue `record_upload_and_verify` for items
  whose latest manifest state is in {upload_failed, stage_failed,
  remote_verify_failed}, when the staged folder is still on disk.

These helpers do not touch the queue or schedule new collection — they only
shepherd uploads that already produced local artifacts.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from aic_collector.automation import manifest as manifest_mod

_ITEM_RE = re.compile(r"^config_(sfp|sc)_(\d+)$")
_REPO_PATH_ITEM_RE = re.compile(r"(?:^|/)config_(sfp|sc)_(\d+)(?:/|$)")

_RETRYABLE_STATES: frozenset[str] = frozenset(
    {"upload_failed", "stage_failed", "remote_verify_failed"}
)


def aggregate_manifests(manifest_paths: Sequence[Path]) -> dict[str, Any]:
    """Combine several PC-local manifests into a single rollup view.

    The latest event for each item_id wins across all input manifests; ties
    (same item_id in two manifests) are resolved by `timestamp` lexicographic
    order, which is ISO-8601 in this codebase so it matches actual time.

    Returns:
      {
        "manifests":   list of input paths (str),
        "items":       {item_id: latest_event_dict},
        "state_counts": {state: count},
        "failures":    [event, ...]   # terminal failure-state events
      }
    """
    items: dict[str, dict[str, Any]] = {}
    for path in manifest_paths:
        for event in manifest_mod.read_events(path):
            item_id = str(event.get("item_id", ""))
            if not item_id:
                continue
            existing = items.get(item_id)
            if existing is None or str(event.get("timestamp", "")) >= str(
                existing.get("timestamp", "")
            ):
                items[item_id] = event

    state_counts: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []
    for event in items.values():
        state = str(event.get("state", ""))
        state_counts[state] += 1
        if state in manifest_mod.FAILURE_STATES:
            failures.append(event)

    return {
        "manifests": [str(p) for p in manifest_paths],
        "items": items,
        "state_counts": dict(state_counts),
        "failures": failures,
    }


def _expected_indices_from_ledger(ledger_path: Path) -> dict[str, set[int]]:
    """For each task_type, return the set of sample_indices the round should see."""
    if not ledger_path.exists():
        return {"sfp": set(), "sc": set()}
    raw = yaml.safe_load(ledger_path.read_text(encoding="utf-8")) or {}
    out: dict[str, set[int]] = {"sfp": set(), "sc": set()}
    for entry in raw.get("entries", []) or []:
        task = entry.get("task_type")
        start = entry.get("start_index")
        count = entry.get("count")
        if task not in out or not isinstance(start, int) or not isinstance(count, int):
            continue
        out[task].update(range(start, start + count))
    return out


def _extract_item_ids(repo_files: Iterable[str]) -> dict[str, set[int]]:
    """Pull (task_type, sample_index) pairs out of repo file paths.

    Repo paths look like `{prefix}/{batch_id}/batch_NNNN/config_sfp_000000/...`
    so we match on any segment beginning with `config_sfp_` or `config_sc_`.
    """
    out: dict[str, set[int]] = {"sfp": set(), "sc": set()}
    for path in repo_files:
        match = _REPO_PATH_ITEM_RE.search(str(path))
        if match is None:
            continue
        task = match.group(1)
        idx = int(match.group(2))
        out.setdefault(task, set()).add(idx)
    return out


def verify_repo_against_ledger(
    *,
    api: Any,
    repo_id: str,
    ledger_path: Path,
    repo_type: str = "dataset",
) -> dict[str, Any]:
    """Compare HF repo file inventory against ledger-derived expected indices.

    `api.list_repo_files(repo_id=..., repo_type=...)` is the only HF call.

    Returns a per-task report:
      {
        "repo_id": str,
        "tasks": {
          "sfp": {"expected": int, "present": int, "missing": [idx], "extra": [idx]},
          "sc":  {...},
        },
        "ok": bool,        # True iff missing == [] and extra == [] for both tasks
      }
    """
    expected = _expected_indices_from_ledger(ledger_path)
    repo_files = list(api.list_repo_files(repo_id=repo_id, repo_type=repo_type))
    found = _extract_item_ids(repo_files)

    tasks: dict[str, dict[str, Any]] = {}
    overall_ok = True
    for task in ("sfp", "sc"):
        exp = expected.get(task, set())
        got = found.get(task, set())
        missing = sorted(exp - got)
        extra = sorted(got - exp)
        tasks[task] = {
            "expected": len(exp),
            "present": len(exp & got),
            "missing": missing,
            "extra": extra,
        }
        if missing or extra:
            overall_ok = False

    return {"repo_id": repo_id, "tasks": tasks, "ok": overall_ok}


def _resolve_local_folder(event: dict[str, Any]) -> Path | None:
    """Pull a still-on-disk staged folder path out of an upload-failure event.

    The worker writes `batch_folder` for batch-mode and `local_folder` /
    `lerobot_path` for per-item mode. We try them in priority order.
    """
    for key in ("batch_folder", "local_folder", "lerobot_path", "run_dir"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return Path(value)
    return None


def retry_failed_uploads(
    *,
    manifest_path: Path,
    repo_id: str,
    api: Any,
    path_prefix: str = "",
    max_attempts: int = 3,
    backoff_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    """Re-issue uploads for items stuck in a retryable failure state.

    Iterates the materialized manifest, picks events whose latest state is in
    `_RETRYABLE_STATES`, finds the staged folder on disk, and re-invokes
    `record_upload_and_verify`. Records each attempt outcome.

    Returns one dict per attempted item:
      {
        "item_id": str,
        "previous_state": str,
        "attempts": int,
        "ok": bool,
        "error": str | None,
        "missing_folder": bool,
      }
    """
    # Local import to avoid a hard dependency on huggingface_hub at module load.
    from aic_collector.automation.batch_runner import record_upload_and_verify

    latest = manifest_mod.materialize(manifest_path)
    report: list[dict[str, Any]] = []

    for item_id, event in latest.items():
        state = str(event.get("state", ""))
        if state not in _RETRYABLE_STATES:
            continue
        folder = _resolve_local_folder(event)
        if folder is None or not folder.exists():
            report.append({
                "item_id": item_id,
                "previous_state": state,
                "attempts": 0,
                "ok": False,
                "error": "staged folder not on disk",
                "missing_folder": True,
            })
            continue

        batch_id = str(event.get("batch_id") or "")
        sub_path = event.get("path_in_repo") or path_prefix or ""

        attempts = 0
        last_err: str | None = None
        ok = False
        for attempts in range(1, max_attempts + 1):
            try:
                remote = record_upload_and_verify(
                    manifest_path=manifest_path,
                    item_id=item_id,
                    batch_id=batch_id,
                    local_folder=folder,
                    repo_id=repo_id,
                    path_in_repo=str(sub_path),
                    api=api,
                )
                ok = bool(remote.get("ok"))
                if ok:
                    last_err = None
                    break
                last_err = "remote verify reported missing files"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempts < max_attempts:
                time.sleep(backoff_seconds * attempts)

        report.append({
            "item_id": item_id,
            "previous_state": state,
            "attempts": attempts,
            "ok": ok,
            "error": last_err,
            "missing_folder": False,
        })

    return report
