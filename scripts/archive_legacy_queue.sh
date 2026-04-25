#!/usr/bin/env bash
# Move stale queue files into an archive directory before starting the
# 2026-04-26 blindspot-fix collection round.
#
# Targets:
#   - 4-digit filenames (config_<task>_NNNN.yaml)  — pre-2026-04-20 layout
#   - 6-digit filenames with index in [0, 100000)  — M0 slot from the
#     2026-04-20 round, which used the old fixed_target preset
#
# Idempotent: missing dirs are skipped; the archive dir is appended to.

set -euo pipefail

ROOT="${1:-configs/train}"
ARCHIVE="${ROOT}/_archive_2026-04-26"
mkdir -p "$ARCHIVE"

moved=0
for task in sfp sc; do
  for state in pending running done failed legacy; do
    src="$ROOT/$task/$state"
    [ -d "$src" ] || continue
    dst="$ARCHIVE/$task/$state"
    mkdir -p "$dst"
    while IFS= read -r -d '' f; do
      name="$(basename "$f")"
      # extract NNNN or NNNNNN
      idx_part="${name#config_${task}_}"
      idx_part="${idx_part%.yaml}"
      [[ "$idx_part" =~ ^[0-9]+$ ]] || continue
      width="${#idx_part}"
      idx=$((10#$idx_part))
      if [ "$width" -eq 4 ]; then
        mv -- "$f" "$dst/$name"
        moved=$((moved + 1))
      elif [ "$width" -ge 6 ] && [ "$idx" -ge 0 ] && [ "$idx" -lt 100000 ]; then
        mv -- "$f" "$dst/$name"
        moved=$((moved + 1))
      fi
    done < <(find "$src" -maxdepth 1 -type f -name "config_${task}_*.yaml" -print0)
  done
done

echo "archived $moved file(s) to $ARCHIVE"
