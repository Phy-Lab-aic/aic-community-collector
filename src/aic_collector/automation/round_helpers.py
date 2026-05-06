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


def _expected_indices_from_ledger(
    ledger_path: Path,
) -> tuple[dict[str, set[int]], list[str]]:
    """For each task_type, return the set of sample_indices the round expects.

    Fail-closed: returns an `errors` list alongside the per-task index sets.
    Callers must treat any non-empty errors list as a verification failure
    (verify_repo_against_ledger does this). A missing file, missing/empty
    `entries`, or any malformed entry is surfaced as an error string instead
    of being silently dropped.
    """
    out: dict[str, set[int]] = {"sfp": set(), "sc": set()}
    errors: list[str] = []

    if not ledger_path.exists():
        errors.append(f"ledger missing: {ledger_path}")
        return out, errors

    try:
        raw = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"ledger YAML error: {exc}")
        return out, errors

    if not isinstance(raw, dict):
        errors.append(f"ledger root must be a mapping: {ledger_path}")
        return out, errors

    entries = raw.get("entries")
    if entries is None:
        errors.append(f"ledger has no `entries` key: {ledger_path}")
        return out, errors
    if not isinstance(entries, list):
        errors.append(f"ledger `entries` must be a list: {ledger_path}")
        return out, errors
    if not entries:
        errors.append(f"ledger `entries` is empty (no work claimed): {ledger_path}")
        return out, errors

    valid_count = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entries[{index}] is not a mapping")
            continue
        task = entry.get("task_type")
        start = entry.get("start_index")
        count = entry.get("count")
        if task not in out:
            errors.append(f"entries[{index}] has unknown task_type: {task!r}")
            continue
        if isinstance(start, bool) or not isinstance(start, int):
            errors.append(f"entries[{index}].start_index is not an int: {start!r}")
            continue
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            errors.append(f"entries[{index}].count is not a non-negative int: {count!r}")
            continue
        out[task].update(range(start, start + count))
        valid_count += 1

    if valid_count == 0:
        errors.append(f"no valid entries found in ledger: {ledger_path}")

    return out, errors


def _file_counts_per_index(repo_files: Iterable[str]) -> dict[str, dict[int, int]]:
    """Bucket repo file paths into {task: {sample_index: file_count}}.

    Files outside any `config_<task>_<NNN>/` segment are ignored, so unrelated
    repo files (top-level READMEs etc.) do not contaminate the count.
    """
    counts: dict[str, dict[int, int]] = {"sfp": {}, "sc": {}}
    for path in repo_files:
        match = _REPO_PATH_ITEM_RE.search(str(path))
        if match is None:
            continue
        task = match.group(1)
        idx = int(match.group(2))
        bucket = counts.setdefault(task, {})
        bucket[idx] = bucket.get(idx, 0) + 1
    return counts


def verify_repo_against_ledger(
    *,
    api: Any,
    repo_id: str,
    ledger_path: Path,
    repo_type: str = "dataset",
    min_files_per_item: int = 1,
) -> dict[str, Any]:
    """Compare HF repo file inventory against ledger-derived expected indices.

    Behaviour:
      - Fails closed on ledger problems (missing/malformed/empty entries).
        Any such error appears in `ledger_errors` and forces `ok: false`.
      - Counts files per sample_index. Indices with `< min_files_per_item`
        files are reported as `below_min_indices` and force `ok: false`.
        Default `min_files_per_item=1` keeps `ok` synonymous with "at least
        one file per expected index"; raise it to your expected per-item
        artifact count to gate on artifact completeness.

    `api.list_repo_files(repo_id=..., repo_type=...)` is the only HF call.

    Returns:
      {
        "repo_id": str,
        "min_files_per_item": int,
        "ledger_errors": [str, ...],
        "tasks": {
          "sfp": {"expected": int, "present": int,
                  "missing": [idx], "extra": [idx],
                  "file_counts": {idx: count, ...},
                  "below_min_indices": [idx, ...]},
          "sc":  {...},
        },
        "ok": bool,
      }
    """
    if min_files_per_item < 1:
        raise ValueError(f"min_files_per_item must be >= 1 (got {min_files_per_item})")

    expected, ledger_errors = _expected_indices_from_ledger(ledger_path)
    repo_files = list(api.list_repo_files(repo_id=repo_id, repo_type=repo_type))
    counts = _file_counts_per_index(repo_files)

    tasks: dict[str, dict[str, Any]] = {}
    coverage_ok = True
    for task in ("sfp", "sc"):
        exp = expected.get(task, set())
        bucket = counts.get(task, {})
        got = set(bucket.keys())
        missing = sorted(exp - got)
        extra = sorted(got - exp)
        below_min = sorted(
            idx for idx in (exp & got) if bucket.get(idx, 0) < min_files_per_item
        )
        tasks[task] = {
            "expected": len(exp),
            "present": len(exp & got),
            "missing": missing,
            "extra": extra,
            "file_counts": {idx: bucket[idx] for idx in sorted(bucket)},
            "below_min_indices": below_min,
        }
        if missing or extra or below_min:
            coverage_ok = False

    return {
        "repo_id": repo_id,
        "min_files_per_item": min_files_per_item,
        "ledger_errors": ledger_errors,
        "tasks": tasks,
        "ok": coverage_ok and not ledger_errors,
    }


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
