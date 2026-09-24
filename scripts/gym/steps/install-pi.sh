#!/usr/bin/env bash
# Install the pi version the production first pass runs, and put it on PATH.
set -euo pipefail

npm install -g "@earendil-works/pi-coding-agent@0.84.3"
echo "$(npm config get prefix)/bin" >> "$GITHUB_PATH"
