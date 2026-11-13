#!/bin/bash
# Build the baseline (Ubuntu 24.04 / Python 3.12 / PostgreSQL 16) image and run the tests.
# Pass "legacy" as the first argument to run the Ubuntu 20.04 compatibility image instead.
# Remaining arguments go to pytest. Requires a running Docker daemon.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${1:-}" = "legacy" ]; then
  shift
  exec scripts/verify-ubuntu2004.sh "$@"
fi
docker build -q -f docker/Dockerfile.ubuntu2404 -t llm-agent-memory:ubuntu2404 .
docker run --rm llm-agent-memory:ubuntu2404 "$@"
