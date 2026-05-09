"""Unit tests for the queue topic migration shipped in c189ad4."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from aic_collector.job_queue.topic_migration import (
    migrate_file,
    migrate_queue_root,
    migrate_text,
)


_OLD_BLOCK = textwrap.dedent(
    """\
      - topic:
          name: /left_camera/image/compressed
          type: sensor_msgs/msg/CompressedImage
      - topic:
          name: /center_camera/image/compressed
          type: sensor_msgs/msg/CompressedImage
      - topic:
          name: /right_camera/image/compressed
          type: sensor_msgs/msg/CompressedImage
    """
)

_NEW_BLOCK = textwrap.dedent(
    """\
      - topic:
          name: /left_camera/image
          type: sensor_msgs/msg/Image
      - topic:
          name: /center_camera/image
          type: sensor_msgs/msg/Image
      - topic:
          name: /right_camera/image
          type: sensor_msgs/msg/Image
    """
)


def test_migrate_text_rewrites_three_camera_pairs() -> None:
    new_text, n = migrate_text(_OLD_BLOCK)
    assert n == 3
    assert new_text == _NEW_BLOCK


def test_migrate_text_is_idempotent() -> None:
    once, n1 = migrate_text(_OLD_BLOCK)
    twice, n2 = migrate_text(once)
    assert n1 == 3
    assert n2 == 0
    assert twice == once


def test_migrate_text_leaves_unrelated_compressed_topics_alone() -> None:
    # A CompressedImage topic that does not match the `<side>_camera/image`
    # anchor must be preserved — the regex is intentionally narrow.
    unrelated = textwrap.dedent(
        """\
          - topic:
              name: /lidar/points/compressed
              type: sensor_msgs/msg/CompressedImage
          - topic:
              name: /diagnostics/image/compressed
              type: sensor_msgs/msg/CompressedImage
        """
    )
    new_text, n = migrate_text(unrelated)
    assert n == 0
    assert new_text == unrelated


def test_migrate_text_does_not_match_name_only_without_type_pair() -> None:
    # If the type line is missing or different, the two-line anchor must
    # refuse to rewrite — this guards against partially-edited files.
    name_only = "  - topic:\n      name: /left_camera/image/compressed\n"
    new_text, n = migrate_text(name_only)
    assert n == 0
    assert new_text == name_only

    wrong_type = textwrap.dedent(
        """\
          - topic:
              name: /left_camera/image/compressed
              type: sensor_msgs/msg/Image
        """
    )
    new_text, n = migrate_text(wrong_type)
    assert n == 0
    assert new_text == wrong_type


def test_migrate_file_rewrites_in_place(tmp_path: Path) -> None:
    cfg = tmp_path / "config_sfp_000001.yaml"
    cfg.write_text("scoring:\n  topics:\n" + _OLD_BLOCK)
    n = migrate_file(cfg)
    assert n == 3
    assert "compressed" not in cfg.read_text()
    assert "/left_camera/image\n" in cfg.read_text()


def test_migrate_file_dry_run_does_not_write(tmp_path: Path) -> None:
    cfg = tmp_path / "config_sfp_000001.yaml"
    original = "scoring:\n  topics:\n" + _OLD_BLOCK
    cfg.write_text(original)
    n = migrate_file(cfg, dry_run=True)
    assert n == 3
    assert cfg.read_text() == original


def test_migrate_queue_root_walks_recursively(tmp_path: Path) -> None:
    sfp_pending = tmp_path / "sfp" / "pending"
    sc_pending = tmp_path / "sc" / "pending"
    sfp_pending.mkdir(parents=True)
    sc_pending.mkdir(parents=True)

    (sfp_pending / "config_sfp_001.yaml").write_text("topics:\n" + _OLD_BLOCK)
    (sfp_pending / "config_sfp_002.yaml").write_text("topics:\n" + _NEW_BLOCK)
    (sc_pending / "config_sc_001.yaml").write_text("topics:\n" + _OLD_BLOCK)
    # Non-config YAML must be ignored even if it contains a matching pair.
    (tmp_path / "ignore_me.yaml").write_text(_OLD_BLOCK)

    files, topics = migrate_queue_root(tmp_path)
    assert files == 2
    assert topics == 6

    # Second invocation is a no-op.
    files2, topics2 = migrate_queue_root(tmp_path)
    assert files2 == 0
    assert topics2 == 0

    # The non-config file is untouched.
    assert "compressed" in (tmp_path / "ignore_me.yaml").read_text()


def test_migrate_queue_root_handles_missing_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    files, topics = migrate_queue_root(missing)
    assert files == 0
    assert topics == 0


@pytest.mark.parametrize("indent", ["  ", "    ", "\t"])
def test_migrate_text_preserves_arbitrary_indentation(indent: str) -> None:
    snippet = (
        f"{indent}name: /left_camera/image/compressed\n"
        f"{indent}type: sensor_msgs/msg/CompressedImage\n"
    )
    expected = (
        f"{indent}name: /left_camera/image\n"
        f"{indent}type: sensor_msgs/msg/Image\n"
    )
    new_text, n = migrate_text(snippet)
    assert n == 1
    assert new_text == expected
