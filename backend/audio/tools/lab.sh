#!/bin/sh
# Run tools/processing_lab.py in the project image with this directory (backend/audio) mounted at /work.
# Relative paths are relative to backend/audio. Example, from WSL:
#   wsl -u root -e sh /path/to/Music-Sync-Web/backend/audio/tools/lab.sh measure .lab/material/programme.wav
# JMM_IMAGE selects the image (default jmm-audio:dev). JMM_UID selects the container
# UID (default 1000): Liquidsoap refuses to run as root.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
image=${JMM_IMAGE:-jmm-audio:dev}
exec docker run --rm --user "${JMM_UID:-1000}:1000" -e HOME=/tmp \
    -v "$repo:/work" -w /work --entrypoint python3 "$image" \
    tools/processing_lab.py "$@"
