#!/bin/bash
# Runs inside a verification image (Ubuntu 24.04 baseline or Ubuntu 20.04 legacy):
# prints the environment, starts the distro PostgreSQL cluster, runs pytest.
set -euo pipefail

echo "== environment =="
grep PRETTY_NAME /etc/os-release
echo "kernel: $(uname -r)  arch: $(uname -m)  cpus: $(nproc)"
python3 --version
python3 -c 'import psycopg2, pytest; print("psycopg2", psycopg2.__version__, "| pytest", pytest.__version__)'
pg_lsclusters

echo "== starting PostgreSQL =="
PGV=$(pg_lsclusters -h | awk '{print $1}' | head -1)
pg_ctlcluster "$PGV" main start -o "-c ssl=off"  # slim image has no snakeoil cert; TLS is P0-5
for _ in $(seq 1 30); do pg_isready -q && break; sleep 1; done
pg_isready
su postgres -c "psql -qAt -c \"CREATE ROLE memory SUPERUSER LOGIN PASSWORD 'memory-test'\""
su postgres -c "psql -qAt -c 'SHOW server_version'"

export TEST_DATABASE_URL="postgresql://memory:memory-test@127.0.0.1:5432/postgres"
echo "== pytest =="
cd /app
python3 -m pytest -v "$@"
