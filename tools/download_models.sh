#!/usr/bin/env bash
# SAM 2.1 hiera-tiny 체크포인트 (~150MB) → models/
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p models
curl -L -o models/sam2.1_hiera_tiny.pt \
  https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt
