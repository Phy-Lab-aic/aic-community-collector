"""Auto-migrate stale camera topics in queued engine configs.

Commit 15117de switched community collection from CompressedImage
(`/X/image/compressed`) to raw `sensor_msgs/msg/Image` (`/X/image`) and
stopped launching the image_transport republish node when
--use-compressed=false. Engine configs generated before that commit
still ask rosbag2 to record the compressed topic; with no republish
publisher, the resulting MCAP has zero camera channels and the
converter rejects the run with "missing required MCAP topics".

The worker calls `migrate_queue_root` at startup so a stale queue
is healed in place before any config is claimed. The two-line
anchor in the regex makes the substitution safe against unrelated
matches, and the operation is idempotent.
"""

from __future__ import annotations

import re
from pathlib import Path

# Match `name: /<side>_camera/image/compressed` immediately followed by
# `type: sensor_msgs/msg/CompressedImage` (with whatever indentation the
# YAML emitter chose). Capturing the indentation around `type:` keeps
# the rewrite formatting-preserving.
_PATTERN = re.compile(
    r"(name:\s*)/(\w+)_camera/image/compressed"
    r"(\s*\n\s*type:\s*)sensor_msgs/msg/CompressedImage"
)
_REPLACEMENT = r"\1/\2_camera/image\3sensor_msgs/msg/Image"


def migrate_text(text: str) -> tuple[str, int]:
    """Return (new_text, n_topics_rewritten)."""
    return _PATTERN.subn(_REPLACEMENT, text)


def migrate_file(path: Path, *, dry_run: bool = False) -> int:
    """Rewrite a single YAML in place. Returns rewritten topic count."""
    text = path.read_text()
    new_text, n = migrate_text(text)
    if n and not dry_run:
        path.write_text(new_text)
    return n


def migrate_queue_root(
    root: Path,
    *,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Walk root recursively and migrate every `config_*.yaml`.

    Returns (files_changed, topics_rewritten).
    """
    files_changed = 0
    topics_rewritten = 0
    if not root.is_dir():
        return (0, 0)
    for path in sorted(root.rglob("config_*.yaml")):
        n = migrate_file(path, dry_run=dry_run)
        if n:
            files_changed += 1
            topics_rewritten += n
    return (files_changed, topics_rewritten)
