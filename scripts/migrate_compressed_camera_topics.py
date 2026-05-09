#!/usr/bin/env python3
"""Rewrite stale camera topics in queued engine configs.

The worker (`aic-collector-worker`) already calls the same migration on
startup via `aic_collector.job_queue.topic_migration.migrate_queue_root`,
so this script is only needed for one-off audits or for users who want
to dry-run before launching the worker.

Usage:
  python scripts/migrate_compressed_camera_topics.py [ROOT] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `src/` importable when run from a checkout without `pip install -e .`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from aic_collector.job_queue.topic_migration import migrate_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path("configs/train"),
        help="Queue root to scan recursively (default: configs/train)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report files that would change without modifying them",
    )
    args = parser.parse_args()

    if not args.root.is_dir():
        sys.stderr.write(f"[error] root not found: {args.root}\n")
        return 1

    total_files = 0
    total_topics = 0
    for path in sorted(args.root.rglob("config_*.yaml")):
        n = migrate_file(path, dry_run=args.dry_run)
        if n:
            total_files += 1
            total_topics += n
            tag = "would migrate" if args.dry_run else "migrated"
            print(f"[{tag}] {path} ({n} camera topics)")

    verb = "would update" if args.dry_run else "updated"
    print(f"=== {verb} {total_files} files, {total_topics} camera topics ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
