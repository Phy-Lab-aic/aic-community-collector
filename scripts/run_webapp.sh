#!/bin/bash
# Launch the AIC Community Collector web UI (Streamlit).
#
# README reference: `uv run src/aic_collector/webapp.py` → http://localhost:8501
#
# Usage:
#   ./scripts/run_webapp.sh            # default: http://localhost:8501
#   ./scripts/run_webapp.sh --help     # pass-through args (if webapp supports any)

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

exec uv run src/aic_collector/webapp.py "$@"
