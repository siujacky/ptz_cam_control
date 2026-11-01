#!/usr/bin/env bash
# Run PTZ Cam in the foreground, as the systemd service does. Extra arguments go to ptzcam,
# e.g. ./run.sh --source /usr/local/hailo/resources/videos/example.mp4 --no-ptz --port 8081
set -euo pipefail
cd "$(dirname "$0")"
export HAILORT_LOGGER_PATH=NONE
exec .venv/bin/python -m ptzcam "$@"
