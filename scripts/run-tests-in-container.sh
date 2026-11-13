#!/bin/bash
# Runs inside the Ubuntu 20.04 image: prints the environment, starts PostgreSQL 12, runs pytest.
set -euo pipefail

echo "== environment =="
grep PRETTY_NAME /etc/os-release
echo "kernel: $(uname -r)  arch: $(uname -m)  cpus: $(nproc)"
python3 --version
python3 -c 'import psycopg2, pytest; print("psycopg2", psycopg2.__version__, "| pytest", pytest.__version__)'
pg_lsclusters

echo "== starting PostgreSQL =="
pg_ctlcluster 12 main start -o "-c ssl=off"  # slim image has no snakeoil cert; TLS is P0-5
for _ in $(seq 1 30); do pg_isready -q && break; sleep 1; done
pg_isready
su postgres -c "psql -qAt -c \"CREATE ROLE memory SUPERUSER LOGIN PASSWORD 'memory-test'\""
su postgres -c "psql -qAt -c 'SHOW server_version'"

export TEST_DATABASE_URL="postgresql://memory:memory-test@127.0.0.1:5432/postgres"
echo "== pytest =="
cd /app
python3 -m pytest -v "$@"
