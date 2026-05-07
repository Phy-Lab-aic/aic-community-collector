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
    hf_repo_id: str | None = None,
    manifest_path: Path | None = None,
    staging_root: Path | None = None,
    lerobot_root: Path | None = None,
    converter_path: Path | None = None,
    hf_path_prefix: str | None = None,
    batch_id: str | None = None,
    upload_batch_size: int | None = None,
    cleanup_after_upload: bool = True,
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
    if hf_repo_id:
        cmd += ["--hf-repo-id", hf_repo_id]
        if manifest_path is not None:
            cmd += ["--automation-manifest", str(manifest_path)]
        if staging_root is not None:
            cmd += ["--staging-root", str(staging_root)]
        if lerobot_root is not None:
            cmd += ["--lerobot-root", str(lerobot_root)]
        if converter_path is not None:
            cmd += ["--converter-path", str(converter_path)]
        if hf_path_prefix:
            cmd += ["--hf-path-prefix", hf_path_prefix]
        if batch_id:
            cmd += ["--batch-id", batch_id]
        if upload_batch_size is not None:
            cmd += ["--upload-batch-size", str(upload_batch_size)]
        cmd += ["--cleanup-after-upload" if cleanup_after_upload else "--no-cleanup-after-upload"]
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


def link_or_copy(src: str | Path, dst: str | Path) -> None:
    """Hardlink large artifacts when possible, falling back to a normal copy."""
    src_path = Path(src)
    dst_path = Path(dst)
    try:
        os.link(src_path, dst_path)
    except OSError:
        shutil.copy2(src_path, dst_path)



def reconcile_queue_results(
    *,
    manifest_path: Path,
    batch_id: str,
    queue_root: Path,
    expected_configs: Sequence[Path],
) -> dict[str, str]:
    """Reconcile expected private-batch configs against done/failed queue dirs."""
    from aic_collector.job_queue.layout import QueueState, queue_dir

    result: dict[str, str] = {}
    for config_path in expected_configs:
        name = config_path.name
        parts = name.removesuffix(".yaml").split("_")
        task_type = parts[1] if len(parts) >= 3 else "sfp"
        item_id = name.removesuffix(".yaml")
        done_path = queue_dir(queue_root, task_type, QueueState.DONE) / name
        failed_path = queue_dir(queue_root, task_type, QueueState.FAILED) / name
        if done_path.exists():
            append_event(
                manifest_path,
                item_id=item_id,
                state="worker_finished",
                batch_id=batch_id,
                queue_path=str(done_path),
            )
            append_event(
                manifest_path,
                item_id=item_id,
                state="reconciled",
                batch_id=batch_id,
                queue_path=str(done_path),
            )
            result[item_id] = "reconciled"
        elif failed_path.exists():
            append_event(
                manifest_path,
                item_id=item_id,
                state="worker_failed",
                batch_id=batch_id,
                queue_path=str(failed_path),
            )
            result[item_id] = "worker_failed"
        else:
            append_event(
                manifest_path,
                item_id=item_id,
                state="reconcile_failed",
                batch_id=batch_id,
                expected_config=str(config_path),
            )
            result[item_id] = "reconcile_failed"
    return result


def validate_run_artifacts(run_dir: Path, *, collect_episode: bool = True) -> dict[str, Any]:
    """Check collected run output before conversion/staging cleanup can proceed."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def validation_passed(validation: dict[str, Any]) -> bool:
        if "ok" in validation or "success" in validation:
            return bool(validation.get("ok", validation.get("success", False)))
        if "passed_count" in validation and "total_count" in validation:
            return int(validation.get("passed_count", -1)) == int(validation.get("total_count", 0))
        raw_checks = validation.get("checks")
        if isinstance(raw_checks, list) and raw_checks:
            return all(bool(item.get("passed", item.get("ok", False))) for item in raw_checks if isinstance(item, dict))
        return False

    check("run_dir", run_dir.exists(), str(run_dir))
    mcap_files = list(run_dir.rglob("*.mcap")) if run_dir.exists() else []
    check("mcap", bool(mcap_files), f"{len(mcap_files)} files")
    tags = run_dir / "tags.json"
    trial_tags = list(run_dir.glob("trial_*_score*/tags.json")) if run_dir.exists() else []
    check("tags", tags.exists() or bool(trial_tags), "tags.json or trial tags")
    validation_path = run_dir / "validation.json"
    if validation_path.exists():
        try:
            validation = json.loads(validation_path.read_text())
            check("validation", validation_passed(validation), str(validation_path))
        except Exception as exc:
            check("validation", False, f"invalid json: {exc}")
    if collect_episode:
        episode_dirs = list(run_dir.rglob("episode")) if run_dir.exists() else []
        check("episode", bool(episode_dirs), "episode directory")
    return {"ok": all(item["ok"] for item in checks), "checks": checks}

def stage_run_artifacts(*, run_dir: Path, staging_root: Path, item_id: str) -> Path:
    """Stage one collected run in the folder layout expected by rosbag-to-lerobot."""
    if not run_dir.exists():
        raise FileNotFoundError(run_dir)
    target = staging_root / item_id
    if target.exists():
        shutil.rmtree(target)
    converter_run = target / item_id
    converter_run.mkdir(parents=True, exist_ok=True)

    bag_source = run_dir / "bag"
    if bag_source.exists():
        copied_mcap = False
        for src in sorted(path for path in bag_source.iterdir() if path.is_file()):
            link_or_copy(src, converter_run / src.name)
            copied_mcap = copied_mcap or src.suffix == ".mcap"
        if not copied_mcap:
            raise FileNotFoundError(f"No MCAP files found under {bag_source}")
    else:
        mcap_files = list(run_dir.rglob("*.mcap"))
        if not mcap_files:
            raise FileNotFoundError(f"No MCAP files found under {run_dir}")
        for mcap in mcap_files:
            link_or_copy(mcap, converter_run / mcap.name)

    for optional in (
        "config.yaml",
        "tags.json",
        "validation.json",
        "policy.txt",
        "seed.txt",
        "scoring_run.yaml",
        "trial_scoring.yaml",
    ):
        src = run_dir / optional
        if src.exists():
            dst_name = "scoring.yaml" if optional == "trial_scoring.yaml" else optional
            link_or_copy(src, converter_run / dst_name)

    episode_source = run_dir / "episode"
    if episode_source.exists():
        shutil.copytree(episode_source, converter_run / "episode", copy_function=link_or_copy)
    return target


def run_converter(*, converter_path: Path, input_path: Path, output_path: Path, config_path: Path | None = None) -> int:
    """Run the rosbag-to-lerobot converter entry point."""
    main_py = converter_path / "src" / "main.py"
    if not main_py.exists():
        raise FileNotFoundError(
            f"rosbag-to-lerobot converter entry point not found: {main_py}. "
            "Install/checkout rosbag-to-lerobot at that path "
            "(for a configured submodule, run `git submodule update --init --recursive "
            "third_party/rosbag-to-lerobot`) or pass --converter-path to an existing checkout."
        )
    output_path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["INPUT_PATH"] = str(input_path)
    env["OUTPUT_PATH"] = str(output_path)
    pythonpath_parts = [
        str(converter_path / "src"),
        str(converter_path / "lerobot" / "src"),
        str(converter_path / "docker" / "torch-stub"),
    ]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    generated_config_path: Path | None = None
    effective_config_path = config_path
    if effective_config_path is None:
        default_config_path = converter_path / "src" / "config.json"
        converter_config = json.loads(default_config_path.read_text())
        task_name = converter_config.get("task") or converter_config.get("task_name") or "aic_task"
        # The worker owns Hugging Face upload/verification.  Keep the converter
        # local-only by avoiding a namespace-style repo_id that triggers its
        # internal push_to_hub path.
        converter_config["repo_id"] = task_name
        generated_config_path = output_path / "_local_converter_config.json"
        generated_config_path.write_text(json.dumps(converter_config), encoding="utf-8")
        effective_config_path = generated_config_path

    cmd = ["uv", "run", "python", str(main_py), str(effective_config_path)]
    try:
        return subprocess.run(cmd, env=env, check=False).returncode
    finally:
        if generated_config_path is not None:
            generated_config_path.unlink(missing_ok=True)


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
    cleanup_paths: Sequence[str | Path] | None = None,
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
            cleanup_paths=[str(path) for path in (cleanup_paths or [local_folder])],
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
    protected_manifest = manifest_path.expanduser().resolve()
    deleted: list[str] = []
    for event in cleanup_ready_items(manifest_path):
        item_id = str(event["item_id"])
        paths = [str(path) for path in event.get("cleanup_paths", [])]
        item_deleted: list[str] = []
        skipped_paths: list[str] = []
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            if not path.exists():
                continue
            resolved_path = path.resolve()
            if resolved_path == protected_manifest or protected_manifest.is_relative_to(resolved_path):
                skipped_paths.append(str(path))
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(str(path))
            item_deleted.append(str(path))
        append_event(
            manifest_path,
            item_id=item_id,
            state="cleanup_done",
            batch_id=event.get("batch_id"),
            deleted_paths=item_deleted,
            skipped_paths=skipped_paths,
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
        hf_repo_id=args.hf_repo_id,
        manifest_path=args.manifest,
        staging_root=args.staging_root,
        lerobot_root=args.staging_root / "lerobot",
        converter_path=args.converter_path,
        hf_path_prefix="automation",
        batch_id=f"automation-{_now_iso()}",
        upload_batch_size=args.batch_size,
        cleanup_after_upload=True,
    )
    if args.dry_run:
        print(json.dumps({"worker_command": command}, ensure_ascii=False))
        return 0

    args.status_file.parent.mkdir(parents=True, exist_ok=True)
    for index in range(args.repeat_count):
        args.status_file.write_text(json.dumps({
            "running": True,
            "repeat_index": index + 1,
            "repeat_count": args.repeat_count,
            "worker_command": command,
            "updated_at": _now_iso(),
        }, ensure_ascii=False))
        resume_uploaded_remote_verification(args.manifest)
        rc = subprocess.run(command, check=False).returncode
        if rc != 0:
            args.status_file.write_text(json.dumps({
                "running": False,
                "status": "failed",
                "return_code": rc,
                "repeat_index": index + 1,
                "repeat_count": args.repeat_count,
                "updated_at": _now_iso(),
            }, ensure_ascii=False))
            return rc

    resume_uploaded_remote_verification(args.manifest)
    args.status_file.write_text(json.dumps({
        "running": False,
        "status": "completed",
        "repeat_count": args.repeat_count,
        "updated_at": _now_iso(),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
