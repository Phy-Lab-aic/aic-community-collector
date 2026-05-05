"""Automation pipeline helpers."""

from aic_collector.automation.manifest import (
    CleanupNotAllowedError,
    ManifestEntry,
    ManifestTransitionError,
    append_event,
    materialize_latest,
    record_cleanup_tombstone,
)

__all__ = [
    "CleanupNotAllowedError",
    "ManifestEntry",
    "ManifestTransitionError",
    "append_event",
    "materialize_latest",
    "record_cleanup_tombstone",
]
