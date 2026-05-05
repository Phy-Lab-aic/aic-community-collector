"""Batch automation runner primitives.

The module intentionally keeps network/destructive actions behind small testable
functions.  The CLI is a resumable supervisor shell around those primitives;
unit tests exercise the safety contracts without contacting Hugging Face.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

try:  # pragma: no cover - exercised with monkeypatch in tests
    from huggingface_hub import HfApi  # type: ignore
except Exception:  # pragma: no cover - dependency may be absent in minimal envs
    HfApi = None  # type: ignore[assignment]

from aic_collector.automation.manifest import (
    append_event,
    cleanup_ready_items,
    latest_event,
    materialize,
    read_events,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_worker_command(
    *,
    queue_root: Path,
    output_root: Path,
    batch_size: int,
    state_file: Path,
    log_file: Path,
    policy: str,
    task: str = "all",
    timeout: int | None = None,
    headless: bool = True,
) -> list[str]:
    """Build an isolated worker command for one automation batch."""
    cmd = [
        "uv",
        "run",
        "aic-collector-worker",
        "--root",
        str(queue_root),
        "--task",
        task,
        "--limit",
        str(batch_size),
        "--policy",
        policy,
        "--collect-episode",
        "true",
        "--output-root",
        str(output_root),
        "--state-file",
        str(state_file),
        "--log",
        str(log_file),
        "--recover",
        "--headless" if headless else "--no-headless",
    ]
    if timeout is not None and timeout > 0:
        cmd += ["--timeout", str(timeout)]
    return cmd


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def folder_inventory(folder: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for file_path in sorted(path for path in folder.rglob("*") if path.is_file()):
        rel = file_path.relative_to(folder).as_posix()
        files.append({"path": rel, "size": file_path.stat().st_size, "sha256": _file_digest(file_path)})
    return {"file_count": len(files), "files": files}


def stage_run_artifacts(*, run_dir: Path, staging_root: Path, item_id: str) -> Path:
    """Copy run artifacts into converter staging without mutating source data."""
    if not run_dir.exists():
        raise FileNotFoundError(run_dir)
    target = staging_root / item_id
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    bag_source = run_dir / "bag"
    if bag_source.exists():
        shutil.copytree(bag_source, target / "bag")
    else:
        mcap_files = list(run_dir.rglob("*.mcap"))
        if not mcap_files:
            raise FileNotFoundError(f"No MCAP files found under {run_dir}")
        (target / "bag").mkdir(parents=True, exist_ok=True)
        for mcap in mcap_files:
            shutil.copy2(mcap, target / "bag" / mcap.name)
    for optional in ("config.yaml", "tags.json", "validation.json"):
        src = run_dir / optional
        if src.exists():
            shutil.copy2(src, target / optional)
    return target


def run_converter(*, converter_path: Path, input_path: Path, output_path: Path, config_path: Path | None = None) -> int:
    """Run the rosbag-to-lerobot converter entry point."""
    main_py = converter_path / "src" / "main.py"
    if not main_py.exists():
        raise FileNotFoundError(main_py)
    output_path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["INPUT_PATH"] = str(input_path)
    env["OUTPUT_PATH"] = str(output_path)
    cmd = ["uv", "run", "python", str(main_py)]
    if config_path is not None:
        cmd += ["--config", str(config_path)]
    return subprocess.run(cmd, env=env, check=False).returncode


def _parse_upload_result(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        revision = result.rstrip("/").split("/")[-1] if result else None
        return {"commit_url": result, "revision": revision}
    data: dict[str, Any] = {}
    for name in ("commit_url", "oid", "commit_hash", "revision"):
        value = getattr(result, name, None)
        if value:
            data[name] = value
    if "revision" not in data:
        data["revision"] = data.get("commit_hash") or data.get("oid")
    return data


def verify_remote_upload(
    *,
    api: Any,
    repo_id: str,
    revision: str | None,
    expected_paths: Sequence[str],
    path_in_repo: str | None = None,
) -> dict[str, Any]:
    """Verify that expected dataset files are listed remotely at a revision."""
    try:
        files = list(api.list_repo_files(repo_id=repo_id, repo_type="dataset", revision=revision))
    except Exception as exc:  # pragma: no cover - defensive network wrapper
        return {"ok": False, "repo_id": repo_id, "revision": revision, "error": str(exc)}
    prefix = f"{path_in_repo.strip('/')}/" if path_in_repo else ""
    expected = [f"{prefix}{path}" if prefix and not path.startswith(prefix) else path for path in expected_paths]
    missing = [path for path in expected if path not in files]
    return {
        "ok": not missing,
        "repo_id": repo_id,
        "revision": revision,
        "files": files,
        "expected": expected,
        "missing": missing,
        "verified_at": _now_iso(),
    }


def record_upload_and_verify(
    *,
    manifest_path: Path,
    item_id: str,
    batch_id: str,
    local_folder: Path,
    repo_id: str,
    path_in_repo: str = "",
    api: Any | None = None,
    expected_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Upload a converted folder, record upload evidence, then verify remote files."""
    api = api or (HfApi() if HfApi is not None else None)
    if api is None:
        raise RuntimeError("huggingface_hub is required for upload")
    inventory = folder_inventory(local_folder)
    result = api.upload_folder(
        repo_id=repo_id,
        repo_type="dataset",
        folder_path=str(local_folder),
        path_in_repo=path_in_repo,
    )
    upload = {
        "repo_id": repo_id,
        "repo_type": "dataset",
        "path_in_repo": path_in_repo,
        "method": "upload_folder",
        "uploaded_at": _now_iso(),
        "inventory": inventory,
        **_parse_upload_result(result),
    }
    append_event(manifest_path, item_id=item_id, state="uploaded", batch_id=batch_id, upload=upload)

    expected = list(expected_paths) if expected_paths is not None else [f["path"] for f in inventory["files"]]
    verification = verify_remote_upload(
        api=api,
        repo_id=repo_id,
        revision=upload.get("revision"),
        expected_paths=expected,
        path_in_repo=path_in_repo,
    )
    if verification.get("ok"):
        append_event(
            manifest_path,
            item_id=item_id,
            state="remote_verified",
            batch_id=batch_id,
            remote=verification,
            cleanup_paths=[str(local_folder)],
        )
    else:
        append_event(manifest_path, item_id=item_id, state="remote_verify_failed", batch_id=batch_id, remote=verification)
    return verification


def resume_uploaded_remote_verification(manifest_path: Path, api: Any | None = None) -> list[str]:
    """Resume items that uploaded before a crash but lack remote verification."""
    api = api or (HfApi() if HfApi is not None else object())
    verified: list[str] = []
    for item_id, event in materialize(manifest_path).items():
        if event.get("state") != "uploaded":
            continue
        upload = event.get("upload") or {}
        inventory = upload.get("inventory") or {}
        expected = [f["path"] for f in inventory.get("files", []) if isinstance(f, dict) and f.get("path")]
        verification = verify_remote_upload(
            api=api,
            repo_id=upload.get("repo_id"),
            revision=upload.get("revision"),
            expected_paths=expected,
            path_in_repo=upload.get("path_in_repo") or "",
        )
        if verification.get("ok"):
            append_event(
                manifest_path,
                item_id=item_id,
                state="remote_verified",
                batch_id=event.get("batch_id"),
                remote=verification,
                cleanup_paths=event.get("cleanup_paths", []),
            )
            verified.append(item_id)
        else:
            append_event(
                manifest_path,
                item_id=item_id,
                state="remote_verify_failed",
                batch_id=event.get("batch_id"),
                remote=verification,
            )
    return verified


def cleanup_verified_paths(manifest_path: Path) -> list[str]:
    """Delete only paths listed by latest remote_verified manifest entries."""
    deleted: list[str] = []
    for event in cleanup_ready_items(manifest_path):
        item_id = str(event["item_id"])
        paths = [str(path) for path in event.get("cleanup_paths", [])]
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            if not path.exists():
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(str(path))
        append_event(
            manifest_path,
            item_id=item_id,
            state="cleanup_done",
            batch_id=event.get("batch_id"),
            deleted_paths=paths,
            deleted_at=_now_iso(),
        )
    return deleted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIC automation batch supervisor")
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--hf-repo-id", required=True)
    parser.add_argument("--queue-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--converter-path", type=Path, default=Path("third_party/rosbag-to-lerobot"))
    parser.add_argument("--worker-state-file", type=Path, default=Path("/tmp/aic_automation_state.json"))
    parser.add_argument("--pid-file", type=Path, default=Path("/tmp/aic_automation_pid.txt"))
    parser.add_argument("--status-file", type=Path, default=Path("/tmp/aic_automation_status.json"))
    parser.add_argument("--log-file", type=Path, default=Path("/tmp/aic_automation_run.log"))
    parser.add_argument("--repeat-count", type=int, default=1)
    parser.add_argument("--policy", default="cheatcode")
    parser.add_argument("--dry-run", action="store_true", help="Print the isolated worker command and exit")
    args = parser.parse_args(argv)

    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    args.queue_root.mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.staging_root.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)

    command = build_worker_command(
        queue_root=args.queue_root,
        output_root=args.output_root,
        batch_size=args.batch_size,
        state_file=args.worker_state_file,
        log_file=Path("/tmp/aic_automation_worker.log"),
        policy=args.policy,
    )
    if args.dry_run:
        print(json.dumps({"worker_command": command}, ensure_ascii=False))
        return 0

    # MVP runtime: resume verification/cleanup primitives are available; full config
    # generation/converter orchestration is intentionally kept in testable helpers.
    resume_uploaded_remote_verification(args.manifest)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
