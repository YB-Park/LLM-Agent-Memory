#!/bin/bash
# Build the Ubuntu 20.04 image and run the test suite in it. Requires a running Docker daemon.
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -q -f docker/Dockerfile.ubuntu2004 -t llm-agent-memory:ubuntu2004 .
docker run --rm llm-agent-memory:ubuntu2004 "$@"
